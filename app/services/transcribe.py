"""🎙️ Transcrição dos áudios do WhatsApp (Groq, modelo Whisper): o texto aparece embaixo do áudio
na conversa (Message.transcript), entra na busca do Inbox e nas regras automáticas como se fosse
uma mensagem escrita (🔥 quis comprar, Qualificado, preço enviado -> Negociando, Perdido...).

Liga por cliente: recurso "transcrever_audios" (⚙ Configurações → Recursos) + a chave GROQ_API_KEY no
servidor (deploy/vps/configurar_groq.sh). Sem os dois, nada é enviado pra fora.
Custo (tabela do Groq): ~US$ 0,04 por hora de áudio. Áudio de mais de MAX_SECONDS fica de fora."""

from __future__ import annotations

import logging
from typing import Optional

import requests

from app import features
from app.config import GROQ_API_KEY

logger = logging.getLogger("transcribe")

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3-turbo"
MAX_SECONDS = 600  # 10 min: áudio maior que isso não costuma ser de cliente
FEATURE = "transcrever_audios"


def is_enabled(tenant) -> bool:
    return bool(GROQ_API_KEY) and features.has(tenant, FEATURE)


def transcribe(content: bytes, mime: str) -> Optional[str]:
    """Texto do áudio (None se falhar). Nunca levanta exceção."""
    ext = "ogg" if "ogg" in (mime or "") or "opus" in (mime or "") else ("mp3" if "mpeg" in (mime or "") else "m4a"
                                                                         if "mp4" in (mime or "") else "ogg")
    try:
        resp = requests.post(URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                             files={"file": (f"audio.{ext}", content, mime or "audio/ogg")},
                             data={"model": MODEL, "language": "pt", "response_format": "json", "temperature": "0"},
                             timeout=60)
    except requests.RequestException as exc:
        logger.warning("transcrição: falha de rede: %s", exc)
        return None
    if not resp.ok:
        logger.warning("transcrição: Groq respondeu %s: %.300s", resp.status_code, resp.text)
        return None
    try:
        return (resp.json().get("text") or "").strip()
    except ValueError:
        return None


def apply_rules(db, lead, message, text: str) -> None:
    """O texto do áudio passa pelas mesmas regras de uma mensagem escrita."""
    from app.services import buying, followup, suggestions

    if lead is None or lead.is_group or not text:
        return
    if message.direction == "in":
        followup.on_inbound_text(db, lead, text)
        buying.on_inbound(lead, text, message.created_at)
        suggestions.on_message(lead, text, outbound=False)
    elif message.direction == "out":
        followup.on_outbound_text(db, lead, text)  # preço falado no áudio: Negociando
        suggestions.on_message(lead, text, outbound=True)


def check_message(message_id: str, rules: bool = True) -> str:
    """Roda em segundo plano depois que o áudio chega (e é copiado). Devolve o texto ("" = nada)."""
    from app.db import SessionLocal
    from app.models import Conversation, Lead, Message, Tenant, WhatsAppNumber
    from app.services import media_store, messaging

    db = SessionLocal()
    try:
        message = db.get(Message, message_id)
        if message is None or message.media_type != "audio" or message.transcript:
            return ""
        conversation = db.get(Conversation, message.conversation_id)
        tenant = db.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None or not is_enabled(tenant):
            return ""
        if message.media_seconds and message.media_seconds > MAX_SECONDS:
            return ""
        content, mime = media_store.load(message.media_stored_key)
        if content is None:
            number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
            content, mime = messaging.fetch_media(number, message.media_id)
        if not content:
            return ""
        db.close()  # a transcrição leva 1-2 s: não segura conexão do banco
        text = transcribe(content, mime)
        if not text:
            return ""
        db = SessionLocal()
        message = db.get(Message, message_id)
        message.transcript = text[:4000]
        if rules:
            lead = db.get(Lead, db.get(Conversation, message.conversation_id).lead_id)
            apply_rules(db, lead, message, text)
        db.commit()
        return text
    except Exception:
        logger.exception("transcrição: falhou na mensagem %s", message_id)
        return ""
    finally:
        db.close()
