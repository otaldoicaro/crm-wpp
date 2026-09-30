"""Webhook da Evolution API (WhatsApp não-oficial). Um só endpoint atende
TODOS os tenants: o payload traz o nome da instância (`instance`), e cada
instância pertence a um WhatsAppNumber (provider="evolution").

Eventos tratados:
- messages.upsert: mensagem nova (do lead, ou enviada pelo app do celular)
- connection.update: número conectou/desconectou (mostra o status na tela WhatsApp)

A URL (com ?token=) é configurada automaticamente em cada instância quando o
número é adicionado pela tela WhatsApp do CRM (ver evolution_client.set_webhook).
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import EVOLUTION_WEBHOOK_TOKEN
from app.db import get_db
from app.models import WhatsAppNumber
from app.services.inbound import ingest_inbound, record_outbound_from_phone

router = APIRouter()
logger = logging.getLogger("evolution_webhook")

MEDIA_TYPES = {
    "imageMessage": ("image", "📷 Imagem"),
    "videoMessage": ("video", "🎥 Vídeo"),
    "audioMessage": ("audio", "🎤 Mensagem de áudio"),
    "documentMessage": ("document", "📄 Documento"),
    "documentWithCaptionMessage": ("document", "📄 Documento"),
    "stickerMessage": ("sticker", "🖼️ Figurinha"),
}


def _normalize_event(name: str) -> str:
    return (name or "").lower().replace("_", ".")


def _phone_from_key(key: dict, data: dict) -> str:
    """Telefone do contato (só dígitos). Contas novas do WhatsApp às vezes vêm
    endereçadas por LID (um id interno, terminado em @lid) em vez do número —
    nesse caso o número real vem num campo alternativo."""
    jid = key.get("remoteJid", "")
    if jid.endswith("@lid"):
        jid = key.get("remoteJidAlt") or key.get("senderPn") or data.get("senderPn") or jid
    return jid.split("@")[0].split(":")[0]


def _parse_content(message: dict) -> tuple:
    """Devolve (texto, media_type, context_info) a partir do objeto `message` do Baileys."""
    if not message:
        return "", "", {}
    if "ephemeralMessage" in message:
        message = message["ephemeralMessage"].get("message", {})
    if "viewOnceMessageV2" in message:
        message = message["viewOnceMessageV2"].get("message", {})

    if "conversation" in message:
        return message["conversation"], "", {}
    if "extendedTextMessage" in message:
        ext = message["extendedTextMessage"]
        return ext.get("text", ""), "", ext.get("contextInfo", {}) or {}

    for field, (media_type, placeholder) in MEDIA_TYPES.items():
        if field in message:
            media = message[field]
            if field == "documentWithCaptionMessage":
                media = media.get("message", {}).get("documentMessage", {})
            caption = media.get("caption", "") or (media.get("fileName", "") if media_type == "document" else "")
            return f"{placeholder}{' — ' + caption if caption else ''}", media_type, media.get("contextInfo", {}) or {}

    kind = next(iter(message.keys()), "desconhecido")
    return f"[mensagem do tipo '{kind}' ainda não suportada]", "", {}


def _referral_from_context(context_info: dict):
    """Anúncio "Clique para WhatsApp" da Meta: no protocolo do WhatsApp Web a
    info do anúncio vem em contextInfo.externalAdReply. Convertemos pro mesmo
    formato do `referral` da Cloud API, pra reaproveitar a mesma atribuição."""
    ad = context_info.get("externalAdReply") or {}
    if not ad:
        return None
    logger.info("evolution: mensagem veio de anúncio: %s", {k: v for k, v in ad.items() if k not in ("thumbnail", "jpegThumbnail")})
    return {
        "source_id": ad.get("sourceId", ""),
        "source_url": ad.get("sourceUrl", ""),
        "headline": ad.get("title", ""),
        "body": ad.get("body", ""),
        "source_type": ad.get("sourceType", ""),
        "ctwa_clid": ad.get("ctwaClid", "") or context_info.get("ctwaClid", ""),
    }


@router.post("/webhooks/evolution")
async def receive_evolution_event(request: Request, db: Session = Depends(get_db)):
    if EVOLUTION_WEBHOOK_TOKEN and request.query_params.get("token") != EVOLUTION_WEBHOOK_TOKEN:
        return JSONResponse({"ok": False}, status_code=403)

    payload = await request.json()
    event = _normalize_event(payload.get("event", ""))
    instance = payload.get("instance", "")
    number = (
        db.query(WhatsAppNumber)
        .filter(WhatsAppNumber.provider == "evolution", WhatsAppNumber.evolution_instance == instance)
        .first()
    )
    if not number:
        logger.warning("evolution: evento %s de instância desconhecida: %s", event, instance)
        return JSONResponse({"ok": True})

    data = payload.get("data") or {}

    if event == "connection.update":
        state = data.get("state", "")
        if state:
            number.connection_state = state
            db.add(number)
            db.commit()
        return JSONResponse({"ok": True})

    if event != "messages.upsert":
        return JSONResponse({"ok": True})

    for item in data if isinstance(data, list) else [data]:
        _handle_message(db, number, item)
    return JSONResponse({"ok": True})


def _handle_message(db: Session, number: WhatsAppNumber, item: dict) -> None:
    key = item.get("key") or {}
    jid = key.get("remoteJid", "")
    if not jid or jid.endswith("@g.us") or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        return  # grupos, status e canais não viram lead

    phone = _phone_from_key(key, item)
    wa_message_id = key.get("id", "")
    body, media_type, context_info = _parse_content(item.get("message") or {})
    if not body and not media_type:
        return  # reação, confirmação de leitura, etc.
    # o Evolution (prepareMessage, v2.3.x) converte extendedTextMessage em
    # "conversation" e move o contextInfo — onde fica o externalAdReply do
    # anúncio — pro nível de cima do payload; por isso ele tem prioridade
    context_info = item.get("contextInfo") or context_info or {}
    media_id = wa_message_id if media_type else ""

    if key.get("fromMe"):
        record_outbound_from_phone(db, number, phone, wa_message_id, body, media_id, media_type)
        return

    ingest_inbound(
        db,
        number,
        from_phone=phone,
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
        profile_name=item.get("pushName", "") or "",
        referral=_referral_from_context(context_info),
    )
