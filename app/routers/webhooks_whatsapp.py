"""Webhook único da WhatsApp Cloud API (Meta). Um só endpoint atende TODOS
os tenants e TODOS os números: a Meta identifica o número de destino em
`metadata.phone_number_id` dentro do payload, e a gente usa isso pra achar
o WhatsAppNumber (e portanto o tenant) correspondente.

Configuração no Meta for Developers > seu App > WhatsApp > Configuration:
- Callback URL: https://SEU_DOMINIO/webhooks/whatsapp
- Verify token: o mesmo valor de META_WEBHOOK_VERIFY_TOKEN no .env
- Webhook fields: marque pelo menos "messages"
"""

import datetime
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.config import META_WEBHOOK_VERIFY_TOKEN
from app.db import get_db
from app.models import Conversation, Lead, Message, PipelineStage, WhatsAppNumber
from app.services import attribution
from app.services.distribution import assign_lead

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
async def receive_whatsapp_event(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue

            number = (
                db.query(WhatsAppNumber)
                .filter(WhatsAppNumber.waba_phone_number_id == phone_number_id)
                .first()
            )
            if not number:
                logger.warning("Mensagem recebida para phone_number_id desconhecido: %s", phone_number_id)
                continue

            contacts = {c["wa_id"]: c for c in value.get("contacts", [])}

            for wa_message in value.get("messages", []):
                _handle_inbound_message(db, number, wa_message, contacts)

    return JSONResponse({"ok": True})


def _handle_inbound_message(db: Session, number: WhatsAppNumber, wa_message: dict, contacts: dict) -> None:
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

    lead = (
        db.query(Lead)
        .filter(Lead.tenant_id == number.tenant_id, Lead.phone == from_phone)
        .first()
    )
    is_new_lead = lead is None

    if is_new_lead:
        profile_name = contacts.get(from_phone, {}).get("profile", {}).get("name", "")
        first_stage = (
            db.query(PipelineStage)
            .filter(PipelineStage.tenant_id == number.tenant_id)
            .order_by(PipelineStage.order)
            .first()
        )
        lead = Lead(
            tenant_id=number.tenant_id,
            name=profile_name,
            phone=from_phone,
            source="whatsapp",
            whatsapp_number_id=number.id,
            stage_id=first_stage.id if first_stage else None,
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)

        if referral:
            attribution.attribution_from_ctwa_referral(db, lead, referral)
        else:
            attribution.attribution_from_click_bridge(db, lead, body)

        assign_lead(db, lead)

    conversation = (
        db.query(Conversation)
        .filter(Conversation.lead_id == lead.id, Conversation.whatsapp_number_id == number.id)
        .first()
    )
    if not conversation:
        conversation = Conversation(
            tenant_id=number.tenant_id,
            lead_id=lead.id,
            whatsapp_number_id=number.id,
            assigned_user_id=lead.assigned_user_id,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    message = Message(
        conversation_id=conversation.id,
        direction="in",
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
    )
    conversation.last_message_at = datetime.datetime.utcnow()
    db.add(message)
    db.add(conversation)
    db.commit()
