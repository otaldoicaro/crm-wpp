"""Envio/recebimento de mídia independente do canal: o Inbox chama estas
funções e elas decidem se falam com a Cloud API oficial ou com o Evolution,
conforme o `provider` do número da conversa."""

from __future__ import annotations

import logging
import threading
import re
from typing import Optional

from app.models import WhatsAppNumber
from app.services import evolution_client, whatsapp_client
from app.services.whatsapp_client import media_kind_for_mime  # noqa: F401  (reexportado pro Inbox)

logger = logging.getLogger("messaging")


UNCONFIRMED = -1  # Message.ack: pediu pro WhatsApp mandar mas a resposta não veio a tempo
FAILED_PREFIX = re.compile(r"^⚠️ (Não enviada|Sem confirmação do WhatsApp)[^:]*: ")

_last = threading.local()


def last_error() -> str:
    """Motivo da última falha de envio nesta thread (pra mostrar na mensagem)."""
    return getattr(_last, "error", "") or "o WhatsApp não aceitou o envio"


def strip_failed(body: str) -> str:
    """Texto original de uma mensagem marcada como não enviada / sem confirmação."""
    return FAILED_PREFIX.sub("", body or "", count=1)


def send_text(number: WhatsAppNumber, phone: str, body: str, quote: Optional[dict] = None) -> tuple:
    """Devolve (ok, id_da_mensagem_no_whatsapp, messageSecret). quote: ver evolution_client.send_text.
    ok=None: o Evolution não respondeu a tempo (servidor lento) — pode ter saído; a confirmação
    chega depois pelo webhook (inbound.record_outbound_from_phone casa com a mensagem)."""
    if number.provider == "evolution":
        try:
            wa_id, secret = evolution_client.send_text(number.evolution_instance, phone, body, quote)
            return True, wa_id, secret
        except evolution_client.EvolutionTimeout:
            return None, "", ""
        except evolution_client.EvolutionError as exc:
            _last.error = friendly_error(exc)
            return False, "", ""
    result = whatsapp_client.send_text_message(number, phone, body)
    wa_id = result.get("body", {}).get("messages", [{}])[0].get("id", "") if result["ok"] else ""
    return result["ok"], wa_id, ""


def send_media(
    number: WhatsAppNumber, phone: str, content: bytes, filename: str, mime_type: str, media_kind: str, caption: str
) -> tuple[bool, str, str, str]:
    """Devolve (ok, id_da_mensagem, media_id_pra_guardar, erro, messageSecret)."""
    if number.provider == "evolution":
        try:
            if media_kind == "audio":  # vai como mensagem de voz (legenda não existe em áudio)
                wa_id, secret = evolution_client.send_audio(number.evolution_instance, phone, content)
            else:
                wa_id, secret = evolution_client.send_media(
                    number.evolution_instance, phone, content, filename, mime_type, media_kind, caption
                )
        except evolution_client.EvolutionTimeout:
            return None, "", "", "", ""  # servidor lento: pode ter saído (confirma pelo webhook)
        except evolution_client.EvolutionError as exc:
            return False, "", "", friendly_error(exc), ""
        # no Evolution a mídia é buscada depois pelo id da própria mensagem
        return True, wa_id, wa_id, "", secret

    media_id = whatsapp_client.upload_media(number, content, filename, mime_type)
    if not media_id:
        return False, "", "", "falha ao enviar arquivo pra Meta", ""
    result = whatsapp_client.send_media_message(number, phone, media_id, media_kind, caption)
    wa_id = result.get("body", {}).get("messages", [{}])[0].get("id", "") if result["ok"] else ""
    return result["ok"], wa_id, media_id, "" if result["ok"] else "Meta recusou o envio", ""


def friendly_error(exc: Exception) -> str:
    """Erro do Evolution em linguagem de vendedor (o detalhe técnico vai pro log)."""
    text = str(exc).lower()
    logger.warning("falha no envio pelo WhatsApp: %s", exc)
    if "does not exist" in text or "connection closed" in text or "not connected" in text or "404" in text:
        return "o WhatsApp deste número está desconectado. Peça pro admin reconectar em WhatsApp."
    if "sem resposta" in text or "timeout" in text:
        return "o servidor do WhatsApp não respondeu. Tente de novo em instantes."
    return "o WhatsApp recusou o envio. Tente de novo; se continuar, avise o admin."


def fetch_media(number: WhatsAppNumber, media_id: str) -> tuple[Optional[bytes], Optional[str]]:
    if number.provider == "evolution":
        return evolution_client.fetch_media(number.evolution_instance, media_id)
    return whatsapp_client.fetch_media(number, media_id)


def expire_unconfirmed(minutes: int = 3) -> int:
    """Mensagem "⏳ confirmando envio" que o WhatsApp não confirmou em X min: avisa pra conferir
    no celular antes de reenviar (não diz "não enviada": pode ter saído e o aviso se perdido)."""
    import datetime

    from app.db import SessionLocal
    from app.models import Message

    db = SessionLocal()
    try:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)
        rows = db.query(Message).filter(Message.ack == UNCONFIRMED, Message.created_at < cutoff).all()
        for m in rows:
            m.ack = 0
            if not m.body.startswith("⚠️"):
                m.body = f"⚠️ Sem confirmação do WhatsApp (confira no celular antes de reenviar): {m.body}"
        db.commit()
        return len(rows)
    finally:
        db.close()


def verify_sent(db, message) -> bool:
    """Confere no Evolution se uma mensagem marcada "não enviada"/"sem confirmação" saiu mesmo.
    Se saiu, marca como enviada (com o id do WhatsApp) e devolve True. Só texto."""
    from app.models import Conversation, WhatsAppNumber

    if message.wa_message_id or message.media_type or message.direction != "out":
        return False
    conversation = db.get(Conversation, message.conversation_id)
    number = db.get(WhatsAppNumber, conversation.whatsapp_number_id) if conversation else None
    if number is None or number.provider != "evolution":
        return False
    text = strip_failed(message.body)
    message.delivery_checks = (message.delivery_checks or 0) + 1
    try:
        wa_id = evolution_client.find_sent_text(number.evolution_instance, text, message.created_at)
    except evolution_client.EvolutionError:
        return False
    if not wa_id:
        return False
    from app.models import Message

    if db.query(Message.id).filter(Message.wa_message_id == wa_id).first():
        return False  # já entrou como outra mensagem (ex: pelo celular)
    message.body, message.wa_message_id, message.ack = text, wa_id, max(message.ack or 0, 1)
    return True


def check_failed_sends() -> int:
    """Em segundo plano: "não enviada"/"sem confirmação" de 1 a 30 min atrás é conferida no Evolution
    (no máximo 2 vezes: ~1 min e ~5 min depois). Saiu? Vira enviada sozinha. Devolve quantas."""
    import datetime

    from app.db import SessionLocal
    from app.models import Message

    db = SessionLocal()
    fixed = 0
    try:
        now = datetime.datetime.utcnow()
        rows = db.query(Message).filter(
            Message.direction == "out", Message.wa_message_id == "", Message.media_type == "",
            Message.body.like("⚠️%"), Message.created_at <= now - datetime.timedelta(minutes=1),
            Message.created_at >= now - datetime.timedelta(minutes=30), Message.delivery_checks < 2,
        ).limit(30).all()
        for m in rows:
            if m.delivery_checks == 1 and m.created_at > now - datetime.timedelta(minutes=5):
                continue  # 2ª conferência só depois de 5 min
            if verify_sent(db, m):
                fixed += 1
        db.commit()
    finally:
        db.close()
    return fixed
