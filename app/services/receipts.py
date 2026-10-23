"""Comprovante de pagamento: quando o cliente manda uma foto/PDF numa negociação, a IA do
Claude diz se é um comprovante (PIX, transferência, boleto pago...) e lê o valor. O CRM NÃO
marca a venda sozinho: mostra no topo da conversa "o cliente mandou um comprovante de
R$ X — confirmar venda?" e o vendedor confirma (ou diz que não é).

Analisa mídia de negócios em aberto que o time já atendeu. A leitura é grátis (texto do PDF /
OCR no servidor, ver receipt_text.py); a IA paga só entra se alguém instalar a chave. Sem ANTHROPIC_API_KEY no
servidor, fica desligado."""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

from app import segments
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
    "Um cliente mandou este arquivo numa conversa de WhatsApp com a loja {store} ({kind}). "
    "Diga se é um COMPROVANTE DE PAGAMENTO já feito (PIX, transferência/TED, boleto pago, cartão aprovado). "
    "Foto de produto, print de conversa, orçamento, nota fiscal ou boleto ainda não pago NÃO são comprovante.\n"
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


def analyze(content: bytes, mime: str, store: str, kind: str = "venda de peças") -> Optional[dict]:
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
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT.format(store=store, kind=kind)}]}],
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


ADD_ON_DAYS = 7  # venda fechada há até 7 dias: comprovante novo pode ser de mais um item da mesma compra


def worth_checking(lead) -> bool:
    """Qualquer negócio em aberto (até em "Novo"): cliente que manda comprovante já comprou,
    mesmo que o CRM não tenha visto a conversa antes (ex: vendedor respondeu pelo celular e a
    mensagem se perdeu). E venda (Ganho) recente: o cliente que leva mais de um item às vezes paga
    cada um separado. A leitura grátis é leve."""
    import datetime

    stage = lead.stage
    if lead.is_group or stage is None or stage.is_lost:
        return False
    if stage.is_won:
        return bool(lead.closed_at and datetime.datetime.utcnow() - lead.closed_at <= datetime.timedelta(days=ADD_ON_DAYS))
    return True


def logged(lead) -> list:
    """[(valor, descrição)] dos comprovantes já contados no valor da venda."""
    out = []
    for line in (lead.receipt_log or "").splitlines():
        value, _, info = line.partition("|")
        try:
            out.append((float(value), info))
        except ValueError:
            continue
    return out


def log_receipt(lead, amount: float, info: str) -> None:
    lead.receipt_log = "\n".join(filter(None, [lead.receipt_log or "", f"{amount:.2f}|{info}"]))[-4000:]


def already_counted(lead, amount: float, info: str) -> bool:
    """O mesmo comprovante mandado de novo (mesmo valor e mesma data/pagador, ou sem como diferenciar)."""
    return any(abs(v - amount) < 0.01 and (i == info or not i or not info) for v, i in logged(lead))


def _brl(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _notify_pending(db, lead, amount: float) -> None:
    """🔔 Comprovante que o CRM não conseguiu confirmar sozinho: avisa o vendedor na tela."""
    from app.services import notify

    notify.push(db, lead.tenant_id, lead.assigned_user_id, "comprovante", "💸 Comprovante a confirmar",
                f"{lead.name or lead.phone} mandou um comprovante de R$ {_brl(amount)}, mas não deu pra confirmar o valor. "
                "Foi venda?", f"/inbox/{lead.id}", lead.id)


def add_on(db, lead, conversation_id: str, message_id: str, amount: float, result: dict) -> None:
    """Comprovante numa venda já fechada: soma no valor da venda (ou pergunta, se não tiver certeza)."""
    from app.models import Message

    info = describe(result)
    if already_counted(lead, amount, info):
        return
    note = None
    if not logged(lead) and lead.deal_value and abs(lead.deal_value - amount) < 0.01:
        log_receipt(lead, amount, info)  # o comprovante da própria venda (fechada pelo preço ou pelo vendedor)
        lead.receipt_status = "confirmed"
    elif logged(lead) and result.get("confidence") == "alta":
        # a venda já foi fechada por comprovante e este é outro, com valor conferido: soma sozinho
        lead.deal_value = round((lead.deal_value or 0) + amount, 2)
        log_receipt(lead, amount, info)
        note = (f"💸 Mais um comprovante de R$ {_brl(amount)}: a venda agora soma R$ {_brl(lead.deal_value)} "
                f"({len(logged(lead))} comprovantes). Se não era da mesma compra, é só corrigir o valor.")
    else:
        lead.receipt_message_id, lead.receipt_amount, lead.receipt_info = message_id, amount, info
        lead.receipt_candidates = ",".join(str(v) for v in (result.get("candidates") or [])[:4])[:120]
        lead.receipt_status = "pending"
        _notify_pending(db, lead, amount)
        note = (f"💸 Mais um comprovante de R$ {_brl(amount)}. Se for de outro item da mesma compra, clique em "
                "“Somar à venda” no topo da conversa.")
    if note:
        db.add(Message(conversation_id=conversation_id, direction="note", body=note))
    db.commit()


def _quoted_prices(db, lead) -> list:
    """Preços que o time mandou nessa conversa (últimos 60 dias) + o último registrado: o valor
    do comprovante costuma bater com um deles."""
    import datetime

    from app.models import Conversation, Message
    from app.services import followup

    since = datetime.datetime.utcnow() - datetime.timedelta(days=60)
    rows = (
        db.query(Message.body)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id == lead.id, Message.direction == "out", Message.created_at >= since)
        .all()
    )
    prices = [v for (body,) in rows for v in followup.find_prices(body or "")]
    if lead.quoted_value:
        prices.append(lead.quoted_value)
    return prices


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
        kind = segments.segment_for(lead.tenant)["sale_kind"]
        lead_id = lead.id
        quoted = _quoted_prices(db, lead)
    finally:
        db.close()  # a análise pode levar alguns segundos: não segura conexão do banco

    # 1º a leitura grátis (texto do PDF / OCR do print do banco, 3 leituras), conferindo o valor com
    # os preços que o time mandou na conversa; a IA paga só entra se estiver ligada e a leitura
    # grátis não tiver certeza
    from app.services import receipt_text

    result = receipt_text.read_receipt(content, mime, quoted)
    if not result.get("candidates") and not result.get("is_payment_receipt"):
        result = None
    if (result is None or result.get("confidence") == "baixa") and ai_enabled():
        result = analyze(content, mime, store, kind)
        if result:
            result.setdefault("candidates", [result.get("amount") or 0])
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
        if lead.stage.is_won:  # venda já fechada: mais um item da mesma compra?
            add_on(db, lead, conversation.id, message_id, amount, result)
            return
        lead.receipt_message_id = message_id
        lead.receipt_amount = amount
        lead.receipt_info = describe(result)
        lead.receipt_candidates = ",".join(str(v) for v in (result.get("candidates") or [])[:4])[:120]
        shown = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        sure = result.get("confidence") == "alta"
        if sure:
            # 🤖 valor conferido (bate com o preço enviado, ou claro e igual nas leituras): fecha sozinho
            from app.services import autostage

            why = "comprovante de pagamento" + (" (valor bate com o preço enviado)" if result.get("matches_quote") else "")
            if autostage.apply(db, lead, "ganho", amount=amount, why=why):
                log_receipt(lead, amount, describe(result))
                db.commit()
                logger.info("receipts: venda de R$ %s marcada sozinha no lead %s", amount, lead_id)
                return
        lead.receipt_status = "pending"
        _notify_pending(db, lead, amount)
        db.add(Message(
            conversation_id=conversation.id, direction="note",
            body=f"💸 Parece um comprovante de pagamento de R$ {shown}. Confira o valor e confirme a venda no topo da conversa.",
        ))
        db.commit()
        logger.info("receipts: comprovante de R$ %s detectado no lead %s", amount, lead_id)
    finally:
        db.close()
