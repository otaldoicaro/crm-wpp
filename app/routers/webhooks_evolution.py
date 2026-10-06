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

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import EVOLUTION_WEBHOOK_TOKEN
from app.db import SessionLocal
from app.models import Message, WhatsAppNumber
from app.services.inbound import ingest_inbound, record_outbound_from_phone
from app.services.msgsecret import decrypt_edit, secret_b64, text_from_message, to_bytes
from app.services.media_store import backup_message_media

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

    if "locationMessage" in message or "liveLocationMessage" in message:
        loc = message.get("locationMessage") or message.get("liveLocationMessage") or {}
        lat, lng = loc.get("degreesLatitude"), loc.get("degreesLongitude")
        name = loc.get("name") or loc.get("address") or ""
        link = f" https://maps.google.com/?q={lat},{lng}" if lat is not None and lng is not None else ""
        return f"📍 Localização{' — ' + name if name else ''}{link}", "", {}
    if "contactMessage" in message:
        return f"👤 Contato: {message['contactMessage'].get('displayName', '')}", "", {}
    if "contactsArrayMessage" in message:
        names = [c.get("displayName", "") for c in message["contactsArrayMessage"].get("contacts", [])]
        return f"👤 Contatos: {', '.join(n for n in names if n)}", "", {}

    kinds = [k for k in message.keys() if k not in IGNORED_TYPES]
    if not kinds:
        return "", "", {}  # só metadados (reação, chave de grupo, etc.): não vira mensagem
    logger.info("evolution: tipo de mensagem não exibido no CRM: %s", kinds)
    return "📎 Mensagem que o CRM ainda não mostra (ex: enquete, figurinha animada) — veja no celular", "", {}


# tipos que não são conteúdo pra mostrar na conversa
IGNORED_TYPES = {
    "messageContextInfo",
    "senderKeyDistributionMessage",
    "reactionMessage",
    "encReactionMessage",
    "pollUpdateMessage",
    "keepInChatMessage",
    "pinInChatMessage",
    "protocolMessage",
    "secretEncryptedMessage",
    "editedMessage",
    "base64",
}


def _author_jids(number: WhatsAppNumber, item: dict) -> list:
    """JIDs possíveis de quem escreveu/editou (número e LID), pra abrir edições cifradas."""
    key = item.get("key") or {}
    if key.get("fromMe"):
        jids = [f"{number.phone_number}@s.whatsapp.net" if number.phone_number else ""]
        jids += [key.get(k, "") for k in ("senderLid", "participantLid")]
    else:
        jids = [key.get(k, "") for k in ("remoteJid", "remoteJidAlt", "senderPn", "senderLid", "participant", "participantAlt")]
    jids.append(item.get("sender", "") if key.get("fromMe") else "")
    return [j for j in jids if j and j.endswith(("@s.whatsapp.net", "@lid"))]


def _apply_edit_or_delete(db: Session, number: WhatsAppNumber, item: dict) -> bool:
    """Edição/exclusão de uma mensagem que já está no CRM. Devolve True se tratou.
    - protocolMessage REVOKE: "Apagar pra todos" -> marca a original como apagada
    - protocolMessage MESSAGE_EDIT / editedMessage: edição com o texto novo -> atualiza
    - secretEncryptedMessage MESSAGE_EDIT: edição criptografada (o texto novo não vem
      pro WhatsApp Web) -> marca a original como editada no celular"""
    message = item.get("message") or {}
    if "editedMessage" in message:
        message = message["editedMessage"].get("message", {}) or message
    proto = message.get("protocolMessage")
    secret = message.get("secretEncryptedMessage")
    if not proto and not secret:
        return False

    target = ((proto or secret).get("key") if proto else secret.get("targetMessageKey")) or {}
    original = db.query(Message).filter(Message.wa_message_id == target.get("id", "")).first() if target else None
    kind = str((proto or {}).get("type", "")) if proto else str(secret.get("secretEncType", ""))

    if original is not None:
        if proto and kind in ("0", "REVOKE"):
            if not original.body.startswith("🚫"):
                original.body = f"🚫 Mensagem apagada no WhatsApp: {original.body}"
        elif proto and (kind in ("14", "MESSAGE_EDIT") or proto.get("editedMessage")):
            new_text, _, _ = _parse_content(proto.get("editedMessage") or {})
            if new_text:
                original.body = f"{new_text} ✏️ (editada)"
        elif secret and kind in ("2", "MESSAGE_EDIT"):
            plain = decrypt_edit(
                original.secret,
                target.get("id", ""),
                _author_jids(number, item),
                to_bytes(secret.get("encPayload")),
                to_bytes(secret.get("encIv")),
            )
            new_text = text_from_message(plain) if plain else ""
            if new_text:
                original.body = f"{new_text} ✏️ (editada)"
            elif "(editada no celular" not in original.body:
                logger.info("evolution: não deu pra abrir a edição de %s (sem secret guardado?)", target.get("id"))
                original.body = f"{original.body} ✏️ (editada no celular — veja o texto novo lá)"
        db.add(original)
        db.commit()
    return True  # nunca vira mensagem nova, mesmo se a original não estiver no CRM


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
async def receive_evolution_event(request: Request, background: BackgroundTasks):
    if EVOLUTION_WEBHOOK_TOKEN and request.query_params.get("token") != EVOLUTION_WEBHOOK_TOKEN:
        return JSONResponse({"ok": False}, status_code=403)
    payload = await request.json()
    # o trabalho com banco roda numa thread separada: se rodasse aqui (no laço
    # principal), cada mensagem recebida travava o CRM inteiro enquanto o banco respondia
    for message_id in await run_in_threadpool(_process_event, payload):
        background.add_task(backup_message_media, message_id)
    return JSONResponse({"ok": True})


def _process_event(payload: dict) -> list:
    """Processa o evento e devolve os ids das mensagens com mídia (pra copiar depois)."""
    db = SessionLocal()
    try:
        return _process_event_db(db, payload)
    finally:
        db.close()


def _process_event_db(db: Session, payload: dict) -> list:
    event = _normalize_event(payload.get("event", ""))
    instance = payload.get("instance", "")
    number = (
        db.query(WhatsAppNumber)
        .filter(WhatsAppNumber.provider == "evolution", WhatsAppNumber.evolution_instance == instance)
        .first()
    )
    if not number:
        logger.warning("evolution: evento %s de instância desconhecida: %s", event, instance)
        return []

    data = payload.get("data") or {}

    if event == "connection.update":
        state = data.get("state", "")
        if state:
            number.connection_state = state
            db.add(number)
            db.commit()
        return []

    if event != "messages.upsert":
        return []

    media_ids = []
    for item in data if isinstance(data, list) else [data]:
        message = _handle_message(db, number, item)
        if message is not None and message.media_id:
            media_ids.append(message.id)
    return media_ids


def _handle_message(db: Session, number: WhatsAppNumber, item: dict):
    key = item.get("key") or {}
    jid = key.get("remoteJid", "")
    if not jid or jid.endswith("@g.us") or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        return None  # grupos, status e canais não viram lead

    if _apply_edit_or_delete(db, number, item):
        return None

    phone = _phone_from_key(key, item)
    wa_message_id = key.get("id", "")
    body, media_type, context_info = _parse_content(item.get("message") or {})
    if not body and not media_type:
        return None  # reação, confirmação de leitura, etc.
    # o Evolution (prepareMessage, v2.3.x) converte extendedTextMessage em
    # "conversation" e move o contextInfo — onde fica o externalAdReply do
    # anúncio — pro nível de cima do payload; por isso ele tem prioridade
    context_info = item.get("contextInfo") or context_info or {}
    media_id = wa_message_id if media_type else ""

    secret = secret_b64(item.get("message") or {})
    if key.get("fromMe"):
        return record_outbound_from_phone(db, number, phone, wa_message_id, body, media_id, media_type, secret=secret)

    return ingest_inbound(
        db,
        number,
        from_phone=phone,
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
        profile_name=item.get("pushName", "") or "",
        referral=_referral_from_context(context_info),
        secret=secret,
    )


def clean_legacy_placeholders() -> int:
    """Versões antigas gravavam "[mensagem do tipo 'X' ainda não suportada]" pra edições,
    reações etc. Apaga as que não são conteúdo e troca o resto pelo texto amigável.
    Roda no start; depois da primeira vez não acha mais nada."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        fixed = 0
        for msg in db.query(Message).filter(Message.body.like("[mensagem do tipo '%' ainda não suportada]")):
            kind = msg.body.split("'")[1]
            if kind in IGNORED_TYPES:
                db.delete(msg)
            else:
                msg.body = "📎 Mensagem que o CRM ainda não mostra (ex: enquete, figurinha animada) — veja no celular"
                db.add(msg)
            fixed += 1
        db.commit()
        return fixed
    finally:
        db.close()
