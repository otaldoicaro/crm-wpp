"""Comprovante de pagamento: quando o cliente manda uma foto/PDF numa negociação, a IA do
Claude diz se é um comprovante (PIX, transferência, boleto pago...) e lê o valor. O CRM NÃO
marca a venda sozinho: mostra no topo da conversa "o cliente mandou um comprovante de
R$ X — confirmar venda?" e o vendedor confirma (ou diz que não é).

Só analisa mídia de leads em negociação (Qualificado em diante, ou que já receberam um preço),
pra não gastar com foto de peça de quem ainda está perguntando. Sem ANTHROPIC_API_KEY no
servidor, fica desligado."""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

from app.config import ANTHROPIC_API_KEY

logger = logging.getLogger("receipts")

MODEL = "claude-opus-5-5"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

SCHEMA = {
    "type": "object",
    "properties": {
        "is_payment_receipt": {"type": "boolean"},
        "amount": {"type": "number"},
        "paid_at": {"type": "string"},
        "method": {"type": "string"},
        "payer": {"type": "string"},
        "payee": {"type": "string"},
        "confidence": {"type": "string", "enum": ["alta", "media", "baixa"]},
    },
    "required": ["is_payment_receipt", "amount", "paid_at", "method", "payer", "payee", "confidence"],
    "additionalProperties": False,
}

PROMPT = (
    "Um cliente mandou este arquivo numa conversa de WhatsApp com a loja {store} (venda de peças). "
    "Diga se é um COMPROVANTE DE PAGAMENTO já feito (PIX, transferência/TED, boleto pago, cartão aprovado). "
    "Foto de peça, print de conversa, orçamento, nota fiscal ou boleto ainda não pago NÃO são comprovante.\n"
    "Se for comprovante, extraia: amount (valor pago em reais, número com ponto decimal, ex: 1250.9), "
    "paid_at (data e hora como aparecem, ex: 07/10/2026 14:32), method (PIX, TED, boleto, cartão...), "
    "payer (quem pagou) e payee (quem recebeu). Campo que não aparecer: string vazia (amount 0). "
    "confidence: alta se o valor e o tipo estão claros; baixa se a imagem está ilegível ou é duvidoso."
)


def ai_enabled() -> bool:
    """IA paga: só se alguém instalou a chave (configurar_ia.sh). Sem ela, só a leitura grátis."""
    return bool(ANTHROPIC_API_KEY)


def is_enabled() -> bool:
    from app.services import receipt_text

    return receipt_text.ocr_available() or ai_enabled()


_client = None


def _api():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=90.0)
    return _client


def _file_block(content: bytes, mime: str) -> Optional[dict]:
    data = base64.standard_b64encode(content).decode()
    if mime in IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    return None


def analyze(content: bytes, mime: str, store: str) -> Optional[dict]:
    """{"is_payment_receipt", "amount", "paid_at", "method", "payer", "payee", "confidence"}
    ou None (tipo de arquivo não suportado, recusa ou erro)."""
    import anthropic

    block = _file_block(content, (mime or "").split(";")[0].strip().lower())
    if block is None:
        return None
    request = dict(
        model=MODEL,
        max_tokens=2000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT.format(store=store)}]}],
    )
    client = _api()
    try:
        try:
            # se a IA recusar por regra de segurança, o próprio servidor da Anthropic tenta outro modelo
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
            )
        except anthropic.BadRequestError:
            response = client.messages.create(**request)  # conta sem o fallback: chamada normal
    except anthropic.APIError as exc:  # limite de uso, rede, erro do servidor: tenta na próxima mídia
        logger.warning("receipts: falha ao analisar: %s", exc)
        return None
    if response.stop_reason == "refusal":
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        logger.warning("receipts: resposta não é JSON: %.200s", text)
        return None


def describe(result: dict) -> str:
    parts = [result.get("method") or "", result.get("paid_at") or ""]
    if result.get("payer"):
        parts.append("de " + result["payer"])
    return " · ".join(p for p in parts if p)[:255]


def worth_checking(lead) -> bool:
    """Lead em negociação (Qualificado em diante, ou já recebeu preço) e ainda em aberto."""
    from app.services import funnel

    stage = lead.stage
    if lead.is_group or stage is None or stage.is_won or stage.is_lost:
        return False
    qualified = funnel.levels(funnel._stages_cached(lead))["qualified"]
    return bool(lead.quoted_value) or (lead.reached_order or 0) >= qualified or stage.order >= qualified


def check_message(message_id: str) -> None:
    """Roda em segundo plano depois que a mídia chega (e é copiada)."""
    if not is_enabled():
        return
    from app.db import SessionLocal
    from app.models import Conversation, Lead, Message, WhatsAppNumber
    from app.services import media_store, messaging

    db = SessionLocal()
    try:
        message = db.get(Message, message_id)
        if message is None or message.direction != "in" or message.media_type not in ("image", "document"):
            return
        conversation = db.get(Conversation, message.conversation_id)
        lead = db.get(Lead, conversation.lead_id) if conversation else None
        if lead is None or not worth_checking(lead):
            return
        content, mime = media_store.load(message.media_stored_key)
        if content is None:
            number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
            content, mime = messaging.fetch_media(number, message.media_id)
        if not content:
            return
        store = lead.tenant.name
        lead_id = lead.id
    finally:
        db.close()  # a análise pode levar alguns segundos: não segura conexão do banco

    # 1º a leitura grátis (texto do PDF / OCR do print do banco); a IA só se estiver ligada e a
    # leitura grátis não tiver certeza
    from app.services import receipt_text

    text = receipt_text.extract_text(content, mime)
    result = receipt_text.classify(text) if text.strip() else None
    if (result is None or result.get("confidence") == "baixa") and ai_enabled():
        result = analyze(content, mime, store)
    if not result or not result.get("is_payment_receipt") or result.get("confidence") == "baixa":
        return
    amount = float(result.get("amount") or 0)
    if amount <= 0:
        return

    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        if lead is None or not worth_checking(lead):
            return
        lead.receipt_message_id = message_id
        lead.receipt_amount = amount
        lead.receipt_info = describe(result)
        lead.receipt_status = "pending"
        shown = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        db.add(Message(
            conversation_id=conversation.id, direction="note",
            body=f"💸 Parece um comprovante de pagamento de R$ {shown}. Confirme a venda no topo da conversa.",
        ))
        db.commit()
        logger.info("receipts: comprovante de R$ %s detectado no lead %s", amount, lead_id)
    finally:
        db.close()
