"""Webhook do UAZapi: mensagens, entregue/lida e conexão dos números com provider="uazapi".

O UAZapi chama POST /webhooks/uazapi/<segredo> (uazapi_client.webhook_url) com um envelope:
{"EventType": "messages" | "messages_update" | "connection" | "call" | "history", "owner", "token",
 "instanceName", "message": {...}, "chat": {...}, "event": {...}, "instance": {...}}

Importante: o UAZapi NÃO repete uma entrega que falhou, então aqui responde rápido (o trabalho com o banco
vai pra uma thread) e tudo é deduplicado pelo id da mensagem. O que escapar, a conferência periódica cobre.

Primeira versão (2026-11): conversas individuais. Grupos e ligações ficam pra depois; o formato exato dos
eventos é registrado no log (UAZAPI_SAMPLE) nas primeiras vezes, pra conferir com um número real."""

from __future__ import annotations

import datetime
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Message, WhatsAppNumber
from app.routers.webhooks_evolution import MEDIA_TYPES, _after_media, _parse_content, _referral_from_context, _remember_lid
from app.services import uazapi_client
from app.services.inbound import ingest_inbound, message_exists, record_outbound_from_phone

logger = logging.getLogger("webhooks_uazapi")
router = APIRouter()

SAMPLES_LEFT = {"n": 30}  # quantos eventos registrar no log com o formato completo (calibragem)
# tipo de mensagem do UAZapi (minúsculo) -> campo do protocolo do WhatsApp (pra reaproveitar MEDIA_TYPES)
TYPE_FIELDS = {field.lower(): field for field in MEDIA_TYPES}
TYPE_FIELDS.update({"image": "imageMessage", "video": "videoMessage", "audio": "audioMessage", "ptt": "audioMessage",
                    "myaudio": "audioMessage", "document": "documentMessage", "sticker": "stickerMessage",
                    "ptv": "ptvMessage", "videoplay": "videoMessage"})
TEXT_TYPES = {"conversation", "extendedtextmessage", "text", "chat"}
STATE = {"connected": "open", "connecting": "connecting", "disconnected": "close", "hibernated": "close"}
ACK = {"delivered": 2, "read": 3, "played": 3}


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "").split("@")[0].split(":")[0] if ch.isdigit())


def _sample(kind: str, payload: dict) -> None:
    if SAMPLES_LEFT["n"] > 0:
        SAMPLES_LEFT["n"] -= 1
        safe = {k: v for k, v in payload.items() if k not in ("token", "owner")}
        logger.warning("UAZAPI_SAMPLE %s: %s", kind, str(safe)[:3000])


@router.post("/webhooks/uazapi/{secret}")
async def receive_uazapi_event(secret: str, request: Request, background: BackgroundTasks):
    if not hmac.compare_digest(secret, uazapi_client.webhook_secret()):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    for message_id in await run_in_threadpool(_process, payload):
        background.add_task(_after_media, message_id)
    return JSONResponse({"ok": True})


def _process(payload: dict) -> list:
    db = SessionLocal()
    try:
        return process_event(db, payload)
    except Exception:
        logger.exception("uazapi: erro ao processar %s", payload.get("EventType"))
        db.rollback()
        return []
    finally:
        db.close()


def _number(db: Session, payload: dict) -> Optional[WhatsAppNumber]:
    query = db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == uazapi_client.PROVIDER)
    token = str(payload.get("token") or "")
    if token:
        found = query.filter(WhatsAppNumber.access_token == token).first()
        if found is not None:
            return found
    name = str(payload.get("instanceName") or (payload.get("instance") or {}).get("name") or "")
    return query.filter(WhatsAppNumber.evolution_instance == name).first() if name else None


def process_event(db: Session, payload: dict) -> list:
    """Processa um evento e devolve os ids (do CRM) das mensagens com mídia, pra copiar depois."""
    kind = str(payload.get("EventType") or payload.get("event") or "").lower()
    number = _number(db, payload)
    if number is None:
        logger.warning("uazapi: evento %s de instância desconhecida (%s)", kind, payload.get("instanceName"))
        return []
    if kind == "connection":
        _apply_connection(db, number, payload)
        return []
    if kind == "messages_update":
        _apply_acks(db, payload.get("event") or {})
        return []
    if kind in ("messages", "history"):
        items = payload.get("messages") if isinstance(payload.get("messages"), list) else [payload.get("message") or {}]
        media_ids = []
        for item in items:
            message = handle_message(db, number, item, payload.get("chat") or {})
            if message is not None and message.media_id:
                media_ids.append(message.id)
        return media_ids
    _sample(kind or "desconhecido", payload)
    return []


def _apply_connection(db: Session, number: WhatsAppNumber, payload: dict) -> None:
    inst = payload.get("instance") or {}
    state = STATE.get(str(inst.get("status") or "").lower())
    if not state or state == number.connection_state:
        return
    logger.warning("uazapi: %s agora %s (%s)", number.label, state, inst.get("lastDisconnectReason") or "")
    number.connection_state = state
    db.add(number)
    db.commit()


def _apply_acks(db: Session, event: dict) -> None:
    """Entregue (✓✓) e lida (✓✓ azul) das NOSSAS mensagens; só pra frente."""
    if not event.get("IsFromMe"):
        return
    states = [str(event.get("Type") or "")] + [str(r.get("state") or "") for r in event.get("Receipts") or []]
    level = max((ACK.get(s.lower(), 0) for s in states), default=0)
    ids = [str(i) for i in event.get("MessageIDs") or [] if i]
    if level < 2 or not ids:
        return
    db.query(Message).filter(Message.wa_message_id.in_(ids), Message.direction == "out", Message.ack < level).update(
        {Message.ack: level}, synchronize_session=False)
    db.commit()


def _contact_phone(db: Session, item: dict, chat: dict) -> str:
    """Telefone (só dígitos) da pessoa da conversa. Conversa por LID: usa o telefone que o UAZapi resolveu
    (chat.phone / sender_pn) e guarda a ligação LID -> telefone pro resto do CRM."""
    chat_id = str(item.get("chatid") or chat.get("wa_chatid") or "")
    if chat_id.endswith("@s.whatsapp.net"):
        phone = _digits(chat_id)
    else:
        phone = _digits(str(chat.get("phone") or "")) or (
            "" if item.get("fromMe") else _digits(str(item.get("sender_pn") or "")))
    lid = str(item.get("sender_lid") if not item.get("fromMe") else "") or str(chat.get("wa_chatlid") or "")
    if not lid and chat_id.endswith("@lid"):
        lid = chat_id
    if phone and lid.endswith("@lid"):
        _remember_lid(db, lid, phone)
    if not phone and chat_id.endswith("@lid"):
        from app.models import LidMap

        known = db.get(LidMap, _digits(chat_id))
        phone = known.phone if known else ""
    return phone


def parse(item: dict) -> tuple:
    """(texto, media_type, context_info) de uma mensagem do UAZapi."""
    content = item.get("content")
    if isinstance(content, dict) and any(k[:1].islower() and k.endswith("Message") or k == "conversation" for k in content):
        return _parse_content(content)  # veio o objeto do protocolo do WhatsApp: mesmo leitor do Evolution
    text = str(item.get("text") or "")
    if isinstance(content, str) and not text:
        text = content
    context = (content.get("contextInfo") if isinstance(content, dict) else None) or {}
    kind = str(item.get("messageType") or "").lower()
    if kind in TEXT_TYPES:
        return text, "", context
    field = TYPE_FIELDS.get(kind)
    if field:
        media_type, placeholder = MEDIA_TYPES[field]
        caption = text or (str(content.get("caption") or content.get("fileName") or "") if isinstance(content, dict) else "")
        return f"{placeholder}{' — ' + caption if caption else ''}", media_type, context
    if text:
        return text, "", context
    return "", "", {}


def handle_message(db: Session, number: WhatsAppNumber, item: dict, chat: dict):
    chat_id = str(item.get("chatid") or chat.get("wa_chatid") or "")
    wa_id = str(item.get("messageid") or item.get("id") or "")
    if not chat_id or not wa_id or item.get("isGroup") or chat_id.endswith(("@g.us", "@broadcast", "@newsletter")):
        return None  # 1ª versão: só conversas individuais
    if message_exists(db, wa_id):
        return None
    body, media_type, context = parse(item)
    if not body and not media_type:
        _sample("sem conteúdo " + str(item.get("messageType")), {"message": item})
        return None
    phone = _contact_phone(db, item, chat)
    if not phone:
        logger.warning("uazapi: conversa %s sem telefone conhecido; mensagem %s fica pra depois", chat_id, wa_id)
        return None
    ts = item.get("messageTimestamp")
    when = None
    if ts:
        try:
            value = int(ts)
            when = datetime.datetime.utcfromtimestamp(value / 1000 if value > 10 ** 11 else value)
        except (TypeError, ValueError):
            when = None
    media_id = wa_id if media_type else ""
    if item.get("fromMe"):
        return record_outbound_from_phone(db, number, phone, wa_id, body, media_id, media_type, when=when)
    return ingest_inbound(
        db, number, from_phone=phone, wa_message_id=wa_id, body=body, media_id=media_id, media_type=media_type,
        profile_name=str(item.get("senderName") or chat.get("wa_name") or ""),
        referral=_referral_from_context(context or {}), when=when,
    )


def catch_up(hours: float = 1.0, max_pages: int = 20) -> dict:
    """Conferência periódica (history_sync chama a cada minuto): busca no UAZapi as mensagens recentes de
    cada número e registra as que o webhook não entregou (o UAZapi não repete entrega que falhou).
    Sem filtro de data na API: vai paginando das mais novas pras mais velhas até passar do período.
    Devolve {nome do número: quantas entraram}."""
    cutoff_ms = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours) - datetime.datetime(1970, 1, 1)).total_seconds() * 1000
    db = SessionLocal()
    result = {}
    try:
        numbers = db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == uazapi_client.PROVIDER,
                                                  WhatsAppNumber.is_active.is_(True), WhatsAppNumber.access_token != "").all()
        for number in numbers:
            added, offset = 0, 0
            try:
                for _ in range(max_pages):
                    page = uazapi_client.find_messages(number.access_token, limit=100, offset=offset)
                    old = False
                    for item in page["messages"]:
                        ts = int(item.get("messageTimestamp") or 0)
                        ts_ms = ts if ts > 10 ** 11 else ts * 1000
                        if ts_ms and ts_ms < cutoff_ms:
                            old = True
                            continue
                        wa_id = str(item.get("messageid") or item.get("id") or "")
                        if not wa_id or message_exists(db, wa_id):
                            continue
                        message = handle_message(db, number, item, {})
                        if message is not None:
                            added += 1
                            if message.media_id:
                                _after_media(message.id)
                    if old or not page["hasMore"]:
                        break
                    offset = page["nextOffset"]
            except uazapi_client.UazapiError as exc:
                logger.warning("uazapi: conferência de %s falhou: %s", number.label, exc)
                db.rollback()
            if added:
                logger.warning("uazapi: %s mensagem(ns) de %s que o webhook não entregou foram recuperadas", added, number.label)
            result[number.label] = added
    finally:
        db.close()
    return result
