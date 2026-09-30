"""Webhook único da WhatsApp Cloud API (Meta). Um só endpoint atende TODOS
os tenants e TODOS os números: a Meta identifica o número de destino em
`metadata.phone_number_id` dentro do payload, e a gente usa isso pra achar
o WhatsAppNumber (e portanto o tenant) correspondente.

Configuração no Meta for Developers > seu App > WhatsApp > Configuration:
- Callback URL: https://SEU_DOMINIO/webhooks/whatsapp
- Verify token: o mesmo valor de META_WEBHOOK_VERIFY_TOKEN no .env
- Webhook fields: marque pelo menos "messages"
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.config import META_WEBHOOK_VERIFY_TOKEN
from app.db import get_db
from app.models import WhatsAppNumber
from app.services.inbound import ingest_inbound
from app.services.media_store import backup_message_media

router = APIRouter()
logger = logging.getLogger("whatsapp_webhook")


@router.get("/webhooks/whatsapp")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and token == META_WEBHOOK_VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/webhooks/whatsapp")
async def receive_whatsapp_event(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    payload = await request.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue

            number = (
                db.query(WhatsAppNumber)
                .filter(WhatsAppNumber.provider == "cloud_api", WhatsAppNumber.waba_phone_number_id == phone_number_id)
                .first()
            )
            if not number:
                logger.warning("Mensagem recebida para phone_number_id desconhecido: %s", phone_number_id)
                continue

            contacts = {c["wa_id"]: c for c in value.get("contacts", [])}

            for wa_message in value.get("messages", []):
                message = _handle_inbound_message(db, number, wa_message, contacts)
                if message is not None and message.media_id:
                    background.add_task(backup_message_media, message.id)

    return JSONResponse({"ok": True})


def _handle_inbound_message(db: Session, number: WhatsAppNumber, wa_message: dict, contacts: dict):
    from_phone = wa_message.get("from", "")
    wa_message_id = wa_message.get("id", "")
    msg_type = wa_message.get("type", "text")
    referral = wa_message.get("referral")

    media_id = ""
    media_type = ""
    if msg_type == "text":
        body = wa_message.get("text", {}).get("body", "")
    elif msg_type in ("audio", "image", "video", "document", "sticker"):
        media = wa_message.get(msg_type, {})
        media_id = media.get("id", "")
        media_type = msg_type
        caption = media.get("caption", "")
        placeholder = {
            "audio": "🎤 Mensagem de áudio",
            "image": "📷 Imagem",
            "video": "🎥 Vídeo",
            "document": "📄 Documento",
            "sticker": "🖼️ Figurinha",
        }[msg_type]
        body = f"{placeholder}{' — ' + caption if caption else ''}"
    else:
        body = f"[mensagem do tipo '{msg_type}' ainda não suportada]"

    profile_name = contacts.get(from_phone, {}).get("profile", {}).get("name", "")
    return ingest_inbound(
        db,
        number,
        from_phone=from_phone,
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
        profile_name=profile_name,
        referral=referral,
    )
