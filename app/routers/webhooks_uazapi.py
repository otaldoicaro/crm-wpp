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
import threading
import time
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

# 📥 número que acabou de conectar: puxa as conversas das últimas 24h que o UAZapi recebeu do celular
# (o WhatsApp manda o histórico recente pro aparelho novo nos primeiros minutos). Roda 2 vezes, porque o
# histórico chega aos poucos; a deduplicação pelo id evita mensagem repetida.
CONNECT_HOURS = 24
CONNECT_DELAYS = (90, 600)
_connected_at: dict = {}  # id do número -> quando agendou (evita agendar 2x: tela e webhook avisam juntos)
# 🔒 o UAZapi entrega as mensagens em paralelo: 3 mensagens seguidas de um cliente novo chegavam juntas e
# cada uma criava um lead (lead duplicado), e a conferência periódica podia gravar de novo uma mensagem que o
# webhook estava gravando. Mensagens da MESMA conversa passam uma de cada vez (conversas diferentes seguem em
# paralelo). 256 travas fixas, escolhidas pelo cliente + conversa.
_chat_locks = [threading.Lock() for _ in range(256)]


def _chat_lock(number: WhatsAppNumber, chat_id: str) -> threading.Lock:
    return _chat_locks[hash((number.tenant_id, _digits(chat_id))) % len(_chat_locks)]


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
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        _apply_self_reads(db, number, event)
        _apply_acks(db, event)
        return []
    if kind == "history" and str(payload.get("event") or "messages") != "messages":
        return []  # lotes de conversas, etiquetas, "terminou": nada pra registrar
    if kind in ("messages", "history"):
        items = payload.get("messages") if isinstance(payload.get("messages"), list) else [payload.get("message") or {}]
        media_ids = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if kind == "history" and _older_than(item, CONNECT_HOURS):
                continue  # histórico antigo do celular: o CRM já tem (e não cria lead de conversa velha)
            message = handle_message(db, number, item, payload.get("chat") or {})
            if message is not None and message.media_id:
                media_ids.append(message.id)
        return media_ids
    if kind == "call":
        _apply_call(db, number, payload)
        return []
    if kind == "contacts":
        _apply_contact(db, number, payload.get("event") or {})
        return []
    if kind == "labels":
        _apply_label_edit(number, payload.get("event") or {})
        return []
    if kind == "chat_labels":
        _apply_chat_labels(db, number, payload.get("chat") or {})
        return []
    _sample(kind or "desconhecido", payload)
    return []


def _older_than(item: dict, hours: float) -> bool:
    try:
        ts = int(item.get("messageTimestamp") or 0)
    except (TypeError, ValueError):
        return True
    ts_s = ts / 1000 if ts > 10 ** 11 else ts
    return ts_s < time.time() - hours * 3600


def _apply_connection(db: Session, number: WhatsAppNumber, payload: dict) -> None:
    inst = payload.get("instance") or {}
    if str(payload.get("type") or "") == "TemporaryBan" or payload.get("temporaryBan"):
        from app.services import antiban  # 🛡️ o WhatsApp bloqueou por um tempo: nada de automática por 48h

        antiban.pause(db, number, antiban.BAN_PAUSE_HOURS, "o WhatsApp avisou bloqueio temporário deste número")
    state = STATE.get(str(inst.get("status") or "").lower())
    if not state or state == number.connection_state:
        return
    logger.warning("uazapi: %s agora %s (%s)", number.label, state, inst.get("lastDisconnectReason") or "")
    was_open = number.connection_state == "open"
    number.connection_state = state
    db.add(number)
    if was_open and state == "close":
        from app.services import wa_health

        wa_health.on_dropped(db, number)  # caiu de verdade (o Desconectar da tela já grava "close" antes)
    db.commit()
    if state == "open":
        on_connected(number.id)  # 📥 acabou de conectar: puxa as conversas das últimas 24h


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


SELF_READ = {"read-self", "readself", "read_self"}


def _apply_self_reads(db: Session, number: WhatsAppNumber, event: dict) -> None:
    """📬 O vendedor leu no celular/WhatsApp Web a mensagem do cliente: a conversa sai das não lidas
    (o mesmo do Evolution). No WhatsApp isso é um recibo "read-self" das mensagens que o cliente mandou."""
    from app.routers.webhooks_evolution import _apply_self_reads as apply_evolution

    kind = str(event.get("Type") or "").lower()
    ids = [str(i) for i in event.get("MessageIDs") or [] if i]
    if kind not in SELF_READ or not ids:
        return
    apply_evolution(db, number, [{"keyId": i, "status": "READ", "fromMe": False} for i in ids])


CALL_TAGS = {"offer": "offer", "accept": "accept", "reject": "reject", "terminate": "terminate", "timeout": "timeout"}


def _apply_call(db: Session, number: WhatsAppNumber, payload: dict) -> None:
    """📞 Ligação do cliente pro WhatsApp do vendedor: mesmo registro do Evolution (tocando → atendida...)."""
    from app.routers.webhooks_evolution import _apply_call as apply_evolution

    if str(payload.get("type") or "") != "Call" or payload.get("fromMe"):
        return  # CallLog (registro final) e ligação que o vendedor fez: ficam de fora por enquanto
    event = payload.get("event") or {}
    tag = str((event.get("Data") or {}).get("Tag") or payload.get("callState") or "").lower()
    status = {"offered": "offer", "accepted": "accept", "rejected": "reject", "terminated": "terminate"}.get(tag, CALL_TAGS.get(tag, ""))
    sender = str(payload.get("sender_pn") or event.get("CallCreator") or event.get("From") or "")
    if event.get("GroupJID") or not event.get("CallID") or not sender:
        return
    apply_evolution(db, number, {"id": event["CallID"], "from": sender, "status": status,
                                 "isVideo": bool(event.get("IsVideo") or (event.get("Data") or {}).get("IsVideo"))})
    db.commit()


def _apply_contact(db: Session, number: WhatsAppNumber, event: dict) -> None:
    """📇 Nome salvo na agenda do celular do vendedor vira o nome do lead (o mesmo do Evolution)."""
    from app.routers.webhooks_evolution import _apply_saved_names

    action = event.get("Action") or {}
    name = str(action.get("fullName") or action.get("firstName") or "")
    jid = str(event.get("JID") or "")
    if jid and name:
        _apply_saved_names(db, number, [{"remoteJid": jid, "name": name}])


def _apply_label_edit(number: WhatsAppNumber, event: dict) -> None:
    from app.services import wa_labels

    action = event.get("Action") or {}
    wa_labels.remember(number.evolution_instance, {"id": event.get("LabelID"), "name": action.get("name"),
                                                   "deleted": action.get("deleted")})


def _label_ids(raw) -> set:
    if isinstance(raw, str):
        raw = raw.strip()
        try:
            import json

            raw = json.loads(raw) if raw.startswith("[") else [x for x in raw.split(",")]
        except ValueError:
            raw = []
    return {str(x).strip() for x in raw or [] if str(x).strip()}


def _apply_chat_labels(db: Session, number: WhatsAppNumber, chat: dict) -> None:
    """🏷️ Etiquetas de uma conversa mudaram: a UAZapi manda a lista completa; o CRM compara com a última
    que guardou (Conversation.wa_labels) pra saber o que foi posto e o que foi tirado."""
    from app.models import Conversation
    from app.services import inbox_state, wa_labels
    from app.services.lead_match import find_lead_by_phone

    chat_id = str(chat.get("wa_chatid") or "")
    if not chat_id or chat_id.endswith(("@g.us", "@broadcast", "@newsletter")) or "wa_label" not in chat:
        return
    phone = _digits(chat_id) if chat_id.endswith("@s.whatsapp.net") else _digits(str(chat.get("phone") or ""))
    lead = find_lead_by_phone(db, number.tenant_id, phone) if phone else None
    if lead is None or lead.is_group or lead.deleted_at or not inbox_state.mirrored(lead):
        return
    conversation = (db.query(Conversation).filter(Conversation.lead_id == lead.id,
                                                  Conversation.whatsapp_number_id == number.id).first())
    if conversation is None:
        return
    new = _label_ids(chat.get("wa_label"))
    first = not conversation.wa_labels  # 1ª vez que o CRM vê as etiquetas desta conversa
    old = _label_ids(conversation.wa_labels) - {"-"}
    conversation.wa_labels = ",".join(sorted(new)) or "-"
    db.add(conversation)
    db.commit()
    if first and len(new) > 1:
        return  # já tinha etiquetas de antes: não dá pra saber qual foi posta agora (não fecha venda velha)
    for label_id in sorted(new - old) + sorted(old - new):
        name = wa_labels.label_name(number, label_id)
        wa_labels.apply(db, lead, number, name, label_id in new)


def _contact_phone(db: Session, item: dict, chat: dict, number: Optional[WhatsAppNumber] = None) -> str:
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
    if not phone and str(item.get("chatlid") or "").endswith("@lid") and not lid:
        lid = str(item["chatlid"])
    if not phone and chat_id.endswith("@lid"):
        from app.models import LidMap

        known = db.get(LidMap, _digits(chat_id))
        phone = known.phone if known else ""
        if not phone and number is not None:  # 🔎 última tentativa: pergunta à UAZapi de quem é esse LID
            phone = uazapi_client.chat_phone(number.access_token, chat_id)
            if phone:
                _remember_lid(db, chat_id, phone)
    return phone


def parse(item: dict) -> tuple:
    """(texto, media_type, context_info) de uma mensagem do UAZapi."""
    content = item.get("content")
    if isinstance(content, dict) and any(k[:1].islower() and k.endswith("Message") or k == "conversation" for k in content):
        return _parse_content(content)  # veio o objeto do protocolo do WhatsApp: mesmo leitor do Evolution
    raw_kind = str(item.get("messageType") or "")
    field = raw_kind[:1].lower() + raw_kind[1:]
    if isinstance(content, dict) and content and field.endswith("Message") and field != "extendedTextMessage":
        body, media_type, context = _parse_content({field: content})  # localização, contato, mídia...: igual ao Evolution
        if body or media_type:
            return body, media_type, context or content.get("contextInfo") or {}
    text = str(item.get("text") or "")
    if isinstance(content, str) and not text:
        text = content
    context = (content.get("contextInfo") if isinstance(content, dict) else None) or {}
    kind = raw_kind.lower()
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
    with _chat_lock(number, chat_id):
        if message_exists(db, wa_id):  # dentro da trava: quem chegar depois já vê a mensagem gravada
            return None
        return _handle_locked(db, number, item, chat, chat_id, wa_id)


_reloaded: dict = {}  # id da mensagem que não abriu -> quando pediu a cópia ao celular
RELOAD_EVERY = 30 * 60


BROKEN_ALERT = 5  # mensagens que não abriram no mesmo WhatsApp em 1h: é o começo do "número surdo"
_broken: dict = {}  # número -> horários das mensagens que não abriram
_broken_warned: dict = {}


def _count_broken(number: WhatsAppNumber) -> None:
    """🔐 Muitas mensagens que não abrem no mesmo WhatsApp em pouco tempo: avisa (e tenta uma retomada
    controlada da conexão), antes que vire "número surdo" sem ninguém perceber."""
    now = time.time()
    times = [t for t in _broken.get(number.id, []) if now - t < 3600] + [now]
    _broken[number.id] = times
    if len(times) < BROKEN_ALERT or now - _broken_warned.get(number.id, 0) < 6 * 3600:
        return
    _broken_warned[number.id] = now
    from app.services import wa_health

    logger.warning("uazapi: %s com %s mensagens que não abriram na última hora", number.label, len(times))
    wa_health.uazapi_reset(number, "mensagens que não abrem")
    db = SessionLocal()
    try:
        fresh = db.get(WhatsAppNumber, number.id)
        if fresh is not None:
            wa_health._warn(db, fresh, wa_health._admins_and_owner(db, fresh), f"🔐 {fresh.label} recebendo mensagens que não abrem",
                            f"Na última hora {len(times)} mensagens chegaram sem abrir nesse WhatsApp (o CRM já pediu de novo ao "
                            "celular). Deixe o WhatsApp aberto no celular, com internet. Se continuar: " + wa_health.RECONNECT_TEXT)
            db.commit()
    finally:
        db.close()


def _ask_copy(number: WhatsAppNumber, wa_id: str) -> None:
    """🔐 Mensagem que chegou sem abrir (o "Bad MAC" que deixava o Evolution surdo): a UAZapi já pede uma
    cópia ao celular sozinha; se não veio, o CRM pede de novo (no máximo a cada 30 min por mensagem).
    Quando a cópia chega, entra como mensagem normal pelo webhook, com o mesmo id."""
    now = time.time()
    if now - _reloaded.get(wa_id, 0) < RELOAD_EVERY:
        return
    _reloaded[wa_id] = now
    if len(_reloaded) > 5000:
        for key in [k for k, v in _reloaded.items() if now - v > RELOAD_EVERY]:
            _reloaded.pop(key, None)
    _count_broken(number)
    try:
        uazapi_client.reload_message(number.access_token, wa_id)
        logger.warning("uazapi: %s recebeu uma mensagem que não abriu (%s): pedida uma cópia ao celular", number.label, wa_id)
    except uazapi_client.UazapiError as exc:
        logger.warning("uazapi: não deu pra pedir a cópia da mensagem %s: %s", wa_id, exc)


def _evolution_item(item: dict, chat_id: str) -> dict:
    """A mesma mensagem no formato do Evolution (pra reaproveitar edição, exclusão e reação de lá)."""
    kind = str(item.get("messageType") or "")
    field = kind[:1].lower() + kind[1:]
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    message = content if field in content else {field: content}
    return {"key": {"id": str(item.get("messageid") or ""), "fromMe": bool(item.get("fromMe")), "remoteJid": chat_id},
            "message": message}


def _special(db: Session, number: WhatsAppNumber, item: dict, chat_id: str) -> bool:
    """Tipos que não viram mensagem nova na conversa. Devolve True se tratou."""
    from app.routers.webhooks_evolution import IGNORED_TYPES, _apply_edit_or_delete, _apply_reaction

    kind = str(item.get("messageType") or "").lower()
    if kind == "error":
        _ask_copy(number, str(item.get("messageid") or item.get("id") or ""))
        return True
    if kind in ("reactionmessage", "reaction") or item.get("reaction"):
        if item.get("fromMe") and item.get("reaction") and item.get("text"):  # vendedor reagiu no celular
            _apply_reaction(db, number, {"key": {"fromMe": True},
                                         "message": {"reactionMessage": {"text": item["text"], "key": {"id": item["reaction"]}}}})
        return True
    if kind in {t.lower() for t in IGNORED_TYPES}:
        _apply_edit_or_delete(db, number, _evolution_item(item, chat_id))  # apagou/editou: atualiza a original
        return True
    return False


def _handle_locked(db: Session, number: WhatsAppNumber, item: dict, chat: dict, chat_id: str, wa_id: str):
    if _special(db, number, item, chat_id):
        return None
    body, media_type, context = parse(item)
    if not body and not media_type:
        _sample("sem conteúdo " + str(item.get("messageType")), {"message": item})
        return None
    phone = _contact_phone(db, item, chat, number)
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
        message = record_outbound_from_phone(db, number, phone, wa_id, body, media_id, media_type, when=when)
    else:
        message = ingest_inbound(
            db, number, from_phone=phone, wa_message_id=wa_id, body=body, media_id=media_id, media_type=media_type,
            profile_name=str(item.get("senderName") or chat.get("wa_name") or ""),
            referral=_referral_from_context(context or {}), when=when,
        )
    return _extras(db, message, item, context or {}, media_type)


def _extras(db: Session, message, item: dict, context: dict, media_type: str):
    """Resposta a outra mensagem (o "responder" do WhatsApp) e duração do áudio, como no Evolution."""
    from app.routers.webhooks_evolution import _set_seconds, _with_quote

    if message is None:
        return None
    quoted = str(item.get("quoted") or context.get("stanzaId") or "")
    if quoted:
        message = _with_quote(db, message, {**context, "stanzaId": quoted})
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    if media_type == "audio":
        try:
            message = _set_seconds(db, message, int(content.get("seconds") or 0))
        except (TypeError, ValueError):
            pass
    return message


def finish_migration(number_id: str) -> bool:
    """📱 O vendedor leu o QR da UAZapi: o Evolution sai (desconecta do celular e apaga a sessão, junto com
    as sobras de "Desconectar 100%"), pra não ficar um aparelho a mais conectado. Devolve True se terminou."""
    from app.services import evolution_client, wa_health

    db = SessionLocal()
    try:
        number = db.get(WhatsAppNumber, number_id)
        if number is None or not number.migrating:
            return False
        number.migrating = False
        db.add(number)
        db.commit()
        root = wa_health.instance_root(number.evolution_instance)
        used = {n for (n,) in db.query(WhatsAppNumber.evolution_instance).filter(WhatsAppNumber.id != number.id)}
        names = [number.evolution_instance]
        try:
            names += [name for name, _state, _owner in evolution_client.list_instances()
                      if name not in used and name != number.evolution_instance and wa_health.instance_root(name) == root]
        except evolution_client.EvolutionError:
            pass
        for name in names:
            for action in (evolution_client.logout, evolution_client.delete_instance):
                try:
                    action(name)
                except evolution_client.EvolutionError:
                    pass
        logger.warning("uazapi: %s passou pra UAZapi; Evolution desligado (%s)", number.label, ", ".join(names))
        return True
    finally:
        db.close()


def on_connected(number_id: str) -> bool:
    """Agenda a busca das últimas 24h do número que acabou de conectar (no máximo 1x a cada 30 min) e
    tira o Evolution de quem estava passando pra UAZapi."""
    threading.Thread(target=_finish_quietly, args=(number_id,), daemon=True).start()
    now = time.time()
    if now - _connected_at.get(number_id, 0) < 1800:
        return False
    _connected_at[number_id] = now
    for delay in CONNECT_DELAYS:
        timer = threading.Timer(delay, _pull_recent, args=(number_id,))
        timer.daemon = True
        timer.start()
    return True


def _finish_quietly(number_id: str) -> None:
    try:
        finish_migration(number_id)
    except Exception:
        logger.exception("uazapi: falhou ao desligar o Evolution de %s", number_id)


def _pull_recent(number_id: str) -> None:
    try:
        result = catch_up(hours=CONNECT_HOURS, max_pages=60, number_id=number_id)
        logger.warning("uazapi: últimas %sh do número recém-conectado: %s", CONNECT_HOURS, result)
    except Exception:
        logger.exception("uazapi: falhou ao puxar as últimas %sh de %s", CONNECT_HOURS, number_id)


def catch_up(hours: float = 1.0, max_pages: int = 20, number_id: str = "") -> dict:
    """Conferência periódica (history_sync chama a cada minuto): busca no UAZapi as mensagens recentes de
    cada número e registra as que o webhook não entregou (o UAZapi não repete entrega que falhou).
    Sem filtro de data na API: vai paginando das mais novas pras mais velhas até passar do período.
    Devolve {nome do número: quantas entraram}."""
    cutoff_ms = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours) - datetime.datetime(1970, 1, 1)).total_seconds() * 1000
    fresh_ms = (time.time() - 120) * 1000
    db = SessionLocal()
    result = {}
    try:
        numbers = db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == uazapi_client.PROVIDER,
                                                  WhatsAppNumber.is_active.is_(True), WhatsAppNumber.access_token != "")
        if number_id:
            numbers = numbers.filter(WhatsAppNumber.id == number_id)
        numbers = numbers.all()
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
                        if ts_ms > fresh_ms:
                            continue  # chegou agora: o webhook ainda pode estar gravando (a próxima rodada pega)
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
