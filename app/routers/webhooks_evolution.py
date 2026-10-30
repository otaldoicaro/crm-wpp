"""Webhook da Evolution API (WhatsApp não-oficial). Um só endpoint atende
TODOS os tenants: o payload traz o nome da instância (`instance`), e cada
instância pertence a um WhatsAppNumber (provider="evolution").

Eventos tratados:
- messages.upsert: mensagem nova (do lead, ou enviada pelo app do celular)
- connection.update: número conectou/desconectou (mostra o status na tela WhatsApp)

A URL (com ?token=) é configurada automaticamente em cada instância quando o
número é adicionado pela tela WhatsApp do CRM (ver evolution_client.set_webhook).
"""

import datetime
import glob
import json
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import EVOLUTION_WEBHOOK_TOKEN
from app.db import SessionLocal
from app.models import Conversation, Lead, Message, WhatsAppNumber
from app.services import calls, evolution_client, inbox_state, receipts, response_times, transcribe
from app.services.inbound import ingest_inbound, message_exists, record_outbound_from_phone
from app.services.msgsecret import decrypt_edit, secret_b64, text_from_message, to_bytes
from app.services.media_store import backup_message_media

router = APIRouter()
logger = logging.getLogger("evolution_webhook")

MEDIA_TYPES = {
    "imageMessage": ("image", "📷 Imagem"),
    "videoMessage": ("video", "🎥 Vídeo"),
    "audioMessage": ("audio", "🎤 Mensagem de áudio"),
    "ptvMessage": ("video", "🎥 Vídeo (bolinha)"),
    "documentMessage": ("document", "📄 Documento"),
    "documentWithCaptionMessage": ("document", "📄 Documento"),
    "stickerMessage": ("sticker", "🖼️ Figurinha"),
}


def _normalize_event(name: str) -> str:
    return (name or "").lower().replace("_", ".")


def _digits(jid: str) -> str:
    return (jid or "").split("@")[0].split(":")[0]


EVO_INSTANCES_DIR = os.getenv("EVO_INSTANCES_DIR", "/evo-instances")


def _lid_from_files(lid: str) -> str:
    """Telefone de um LID lido direto dos arquivos do WhatsApp conectado (lid-mapping-<lid>_reverse.json
    em cada instância do Evolution). Na hora, sem esperar a importação de 15 em 15 min."""
    if not lid.isdigit() or not os.path.isdir(EVO_INSTANCES_DIR):
        return ""
    for path in glob.glob(os.path.join(EVO_INSTANCES_DIR, "*", f"lid-mapping-{lid}_reverse.json")):
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError):
            continue
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if 8 <= len(digits) <= 15:
            return digits
    return ""


_LID_MISSES: dict = {}  # lid -> quando falhou (não repete a consulta/aviso por 1h)
_LID_RETRY = 3600


def _remember_lid(db: Session, lid_jid: str, phone: str) -> None:
    from app.models import LidMap

    lid = _digits(lid_jid)
    if lid and phone and not db.get(LidMap, lid):
        db.add(LidMap(lid=lid, phone=phone))
        db.commit()  # guarda já: mesmo se esta mensagem for ignorada, a próxima acha o lead


def _phone_from_key(key: dict, data: dict, db: Optional[Session] = None, number: Optional[WhatsAppNumber] = None) -> str:
    """Telefone do contato (só dígitos). O WhatsApp está endereçando parte das conversas por
    LID (um id interno, "123...@lid") em vez do número. O número real costuma vir num campo
    alternativo; quando não vem (comum nas mensagens que o vendedor manda pelo CELULAR), o CRM
    procura no que já aprendeu (lid_map) e, se preciso, pergunta ao Evolution. Sem isso a
    mensagem não achava o lead e era descartada."""
    jid = key.get("remoteJid", "")
    alt = key.get("remoteJidAlt") or ""
    if not jid.endswith("@lid"):
        if db is not None and alt.endswith("@lid"):
            _remember_lid(db, alt, _digits(jid))  # conversa por número que também tem LID: aprende
        return _digits(jid)
    pn = next((j for j in (alt, key.get("senderPn"), data.get("senderPn")) if j and not j.endswith("@lid")), "")
    if pn:
        if db is not None:
            _remember_lid(db, jid, _digits(pn))
        return _digits(pn)
    if db is not None:
        from app.models import LidMap

        known = db.get(LidMap, _digits(jid))
        if known:
            return known.phone
        from app.services import lid_store

        # tabela do próprio WhatsApp: no Redis do Evolution ou na pasta montada no CRM
        phone = lid_store.phone_for(_digits(jid)) or _lid_from_files(_digits(jid))
        if phone:
            _remember_lid(db, jid, phone)
            return phone
        lid = _digits(jid)
        now = time.monotonic()
        if now - _LID_MISSES.get(lid, -_LID_RETRY) < _LID_RETRY:
            return lid  # já tentou há pouco: não pergunta de novo nem repete o aviso
        if number is not None and number.provider == "evolution":
            phone = evolution_client.resolve_lid(number.evolution_instance, jid)
            if phone:
                _remember_lid(db, jid, phone)
                return phone
        _LID_MISSES[lid] = now
        logger.warning("evolution: conversa por LID sem telefone conhecido (%s); a importação de LIDs (a cada 15 min) resolve", lid)
    return _digits(jid)


def _parse_content(message: dict) -> tuple:
    """Devolve (texto, media_type, context_info) a partir do objeto `message` do Baileys."""
    if not message:
        return "", "", {}
    # embrulhos (mensagem temporária, "ver uma vez", foto de álbum, enviada de outro aparelho...)
    for _ in range(4):
        wrapper = next((w for w in WRAPPERS if w in message and isinstance(message[w], dict)), None)
        if wrapper is None:
            break
        message = message[wrapper].get("message") or {}

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
    text = _other_text(message)
    if text:
        return text, "", {}

    kinds = [k for k in message.keys() if k not in IGNORED_TYPES]
    if not kinds:
        return "", "", {}  # só metadados (reação, chave de grupo, etc.): não vira mensagem
    logger.info("evolution: tipo de mensagem não exibido no CRM: %s", kinds)
    return UNSUPPORTED_TEXT, "", {}


EDIT_MARK = "✏️ (editada no celular — veja o texto novo lá)"
UNSUPPORTED_TEXT = "📎 Mensagem que o CRM ainda não mostra (ex: enquete, figurinha animada) — veja no celular"
WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension",
            "associatedChildMessage", "deviceSentMessage", "futureProofMessage", "botInvokeMessage",
            "groupMentionedMessage")


def _other_text(message: dict) -> str:
    """Texto pra tipos sem mídia que o CRM mostra resumidos (enquete, botão, convite, ligação...)."""
    for key in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3", "pollCreationMessageV4"):
        if key in message:
            poll = message[key] or {}
            options = [o.get("optionName", "") for o in poll.get("options") or []]
            return f"📊 Enquete: {poll.get('name', '')}" + (f" — opções: {' / '.join(o for o in options if o)}" if options else "")
    if "eventMessage" in message:
        ev = message["eventMessage"] or {}
        return f"📅 Evento: {ev.get('name', '')}" + (f" — {ev['description']}" if ev.get("description") else "")
    if "buttonsResponseMessage" in message:
        return f"🔘 {message['buttonsResponseMessage'].get('selectedDisplayText', '') or 'Respondeu um botão'}"
    if "templateButtonReplyMessage" in message:
        return f"🔘 {message['templateButtonReplyMessage'].get('selectedDisplayText', '') or 'Respondeu um botão'}"
    if "listResponseMessage" in message:
        item = message["listResponseMessage"] or {}
        return f"🔘 {item.get('title', '') or (item.get('singleSelectReply') or {}).get('selectedRowId', '') or 'Escolheu uma opção da lista'}"
    if "interactiveResponseMessage" in message:
        return f"🔘 {((message['interactiveResponseMessage'] or {}).get('body') or {}).get('text', '') or 'Respondeu um botão'}"
    if "buttonsMessage" in message:
        return message["buttonsMessage"].get("contentText", "") or "🔘 Mensagem com botões"
    if "templateMessage" in message:
        tpl = message["templateMessage"] or {}
        hyd = tpl.get("hydratedTemplate") or tpl.get("hydratedFourRowTemplate") or {}
        return hyd.get("hydratedContentText", "") or "🔘 Mensagem com botões"
    if "interactiveMessage" in message:
        return ((message["interactiveMessage"] or {}).get("body") or {}).get("text", "") or "🔘 Mensagem com botões"
    if "listMessage" in message:
        lst = message["listMessage"] or {}
        return " — ".join(x for x in (lst.get("title", ""), lst.get("description", "")) if x) or "🔘 Lista de opções"
    if "groupInviteMessage" in message:
        return f"👥 Convite pro grupo: {message['groupInviteMessage'].get('groupName', '')}"
    if "productMessage" in message:
        prod = (message["productMessage"] or {}).get("product") or {}
        return f"🛍️ Produto: {prod.get('title', '')}"
    if "orderMessage" in message:
        return f"🛒 Pedido{': ' + message['orderMessage'].get('message', '') if message['orderMessage'].get('message') else ''}"
    if "lottieStickerMessage" in message:
        return "🖼️ Figurinha animada (abre só no celular)"
    if "stickerPackMessage" in message:
        return "🖼️ Pacote de figurinhas"
    if "callLogMesssage" in message:  # texto final em calls.text (quem ligou, atendida, duração)
        return "📞 Ligação pelo WhatsApp"
    if "call" in message or "scheduledCallCreationMessage" in message:
        return "📞 Ligação pelo WhatsApp"
    if "requestPhoneNumberMessage" in message:
        return "📱 Pediu seu número de telefone"
    return ""


# tipos que não são conteúdo pra mostrar na conversa
IGNORED_TYPES = {
    "albumMessage",  # só avisa "vem um álbum"; as fotos chegam uma a uma
    "messageHistoryBundle",
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
    if key.get("fromMe"):
        jids += _own_jids(number)
    return [j for j in jids if j and j.endswith(("@s.whatsapp.net", "@lid"))]


def _own_jids(number: WhatsAppNumber) -> list:
    """Número e LID do próprio WhatsApp conectado (creds.json da instância no Evolution): quem
    edita pelo celular do vendedor assina a edição com um deles."""
    path = os.path.join(EVO_INSTANCES_DIR, number.evolution_instance or "-", "creds.json")
    try:
        with open(path, encoding="utf-8") as f:
            me = (json.load(f) or {}).get("me") or {}
    except (OSError, ValueError):
        return []
    out = []
    for raw, server in ((me.get("id", ""), "s.whatsapp.net"), (me.get("lid", ""), "lid")):
        digits = _digits(raw)
        if digits:
            out.append(f"{digits}@{server}")
    return out


def jid_variants(db: Session, jids: list) -> list:
    """Cada JID nos dois formatos (número e LID), pelo que o CRM já aprendeu (lid_map) e pelos
    arquivos do Evolution: o WhatsApp assina a edição com um ou outro."""
    from app.models import LidMap

    out = list(jids)
    for jid in jids:
        digits = _digits(jid)
        if not digits:
            continue
        if jid.endswith("@lid"):
            row = db.get(LidMap, digits)
            phone = row.phone if row else _lid_from_files(digits)
            if phone:
                out.append(f"{phone}@s.whatsapp.net")
        else:
            row = db.query(LidMap).filter(LidMap.phone == digits).first()
            lid = row.lid if row else _lid_from_phone_file(digits)
            if lid:
                out.append(f"{lid}@lid")
    return list(dict.fromkeys(out))


def _lid_from_phone_file(phone: str) -> str:
    for path in glob.glob(os.path.join(EVO_INSTANCES_DIR, "*", f"lid-mapping-{phone}.json")):
        try:
            with open(path, encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError):
            continue
        digits = _digits(str(value))
        if digits:
            return digits
    return ""


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
                original.body, original.edited_at = new_text, datetime.datetime.utcnow()
        elif secret and kind in ("2", "MESSAGE_EDIT"):
            jids = jid_variants(db, _author_jids(number, item))
            payload, iv = to_bytes(secret.get("encPayload")), to_bytes(secret.get("encIv"))
            plain = decrypt_edit(original.secret, target.get("id", ""), jids, payload, iv)
            new_text = text_from_message(plain) if plain else ""
            if new_text:
                original.body, original.edited_at, original.pending_edit = new_text, datetime.datetime.utcnow(), ""
            else:
                logger.info("evolution: não deu pra abrir a edição de %s (secret guardado: %s; %d JIDs tentados)",
                            target.get("id"), bool(original.secret), len(jids))
                original.pending_edit = json.dumps({"payload": secret.get("encPayload"), "iv": secret.get("encIv"), "jids": jids})
                if EDIT_MARK not in original.body:
                    original.body = f"{original.body} {EDIT_MARK}"
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
    if (_normalize_event(payload.get("event", "")) == "messages.update" and not _self_read_updates(payload.get("data"))
            and not _ack_updates(payload.get("data"))):
        return JSONResponse({"ok": True})
    # o trabalho com banco roda numa thread separada: se rodasse aqui (no laço
    # principal), cada mensagem recebida travava o CRM inteiro enquanto o banco respondia
    for message_id in await run_in_threadpool(_process_event, payload):
        background.add_task(_after_media, message_id)
    return JSONResponse({"ok": True})


READ_STATUSES = {"READ", "PLAYED"}
# nossas mensagens: entregue (✓✓) e lida pelo cliente (✓✓ azul)
ACK_LEVEL = {"SERVER_ACK": 1, "DELIVERY_ACK": 2, "READ": 3, "PLAYED": 3}


def _ack_updates(data) -> list:
    items = data if isinstance(data, list) else [data or {}]
    return [i for i in items if isinstance(i, dict) and i.get("fromMe") and i.get("keyId") and ACK_LEVEL.get(i.get("status"), 0) >= 2]


def _apply_acks(db: Session, data) -> None:
    """Uma atualização por mensagem, só pra frente (lida não volta pra entregue)."""
    for item in _ack_updates(data):
        level = ACK_LEVEL[item["status"]]
        db.query(Message).filter(Message.wa_message_id == item["keyId"], Message.direction == "out", Message.ack < level).update(
            {Message.ack: level}, synchronize_session=False
        )
    db.commit()


def _self_read_updates(data) -> list:
    """Leituras feitas pelo próprio vendedor (abriu a conversa no celular): status READ
    numa mensagem que o CLIENTE mandou. O resto (entregue/lido pelo cliente) é ignorado."""
    items = data if isinstance(data, list) else [data or {}]
    return [
        i for i in items
        if isinstance(i, dict) and not i.get("fromMe") and i.get("status") in READ_STATUSES
        and i.get("keyId")
    ]


def _apply_self_reads(db: Session, number: WhatsAppNumber, data) -> None:
    """Marca "visto pelo vendedor" nos leads (e grupos) cujas mensagens ele leu no celular."""
    for item in _self_read_updates(data):
        message = db.query(Message).filter(Message.wa_message_id == item["keyId"], Message.direction == "in").first()
        if message is None:
            continue
        conversation = db.get(Conversation, message.conversation_id)
        lead = db.get(Lead, conversation.lead_id) if conversation and conversation.tenant_id == number.tenant_id else None
        if lead is None:
            continue
        # leu a última mensagem do cliente: visto agora; leu uma antiga: visto até ela
        newest = lead.last_inbound_at is None or message.created_at >= lead.last_inbound_at - datetime.timedelta(seconds=5)
        inbox_state.mark_seen_by_seller(lead, datetime.datetime.utcnow() if newest else message.created_at)
    db.commit()


def _apply_reaction(db: Session, number: WhatsAppNumber, item: dict) -> bool:
    """Vendedor reagiu (👍, 🙏...) pelo celular a uma mensagem do cliente: conta como
    "atendimento encerrado" — o cliente só agradeceu, não precisa de resposta em texto."""
    reaction = (item.get("message") or {}).get("reactionMessage")
    if not reaction:
        return False
    if (item.get("key") or {}).get("fromMe") and reaction.get("text"):
        target = (reaction.get("key") or {}).get("id", "")
        message = db.query(Message).filter(Message.wa_message_id == target).first() if target else None
        conversation = db.get(Conversation, message.conversation_id) if message else None
        if conversation and conversation.tenant_id == number.tenant_id:
            lead = db.get(Lead, conversation.lead_id)
            if lead and not lead.is_group:
                response_times.settle(lead, number.owner_user_id)
                inbox_state.mark_seen_by_seller(lead)
                db.commit()
    return True


def _after_media(message_id: str) -> None:
    """Em segundo plano: copia a mídia e, se for de um lead em negociação, vê se é comprovante."""
    backup_message_media(message_id)
    receipts.check_message(message_id)
    transcribe.check_message(message_id)  # 🎙️ áudio vira texto (só cliente com o recurso ligado)


def _apply_saved_names(db: Session, number: WhatsAppNumber, data) -> None:
    """📇 Contato salvo na agenda do celular do vendedor: o WhatsApp sincroniza a agenda com o
    aparelho conectado (contacts.upsert) e o CRM passa a usar esse nome no lead, igual no WhatsApp.
    Nome que é só o número, ou igual ao que já está, não muda nada. Sem contato salvo: fica como está."""
    from app.services.lead_match import find_lead_by_phone

    items = data if isinstance(data, list) else [data]
    changed = False
    for item in items:
        jid = str(item.get("remoteJid") or item.get("id") or "")
        name = " ".join(str(item.get("name") or item.get("pushName") or "").split())[:160]
        if not jid.endswith("@s.whatsapp.net") or not name:
            continue
        phone = _digits(jid)
        if not phone or _digits(name) == phone or name.replace("+", "").replace(" ", "").isdigit():
            continue  # o WhatsApp manda o próprio número quando não tem nome salvo
        lead = find_lead_by_phone(db, number.tenant_id, phone)
        if lead is None or lead.is_group or lead.name == name:
            continue
        if number.owner_user_id and lead.assigned_user_id and lead.assigned_user_id != number.owner_user_id:
            continue  # agenda de outro vendedor: não renomeia o lead de quem atende
        lead.name = name
        changed = True
    if changed:
        db.commit()


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
            if number.connection_state == "open" and state == "close":
                from app.services import wa_health

                wa_health.on_dropped(db, number)  # 📵 caiu: avisa o vendedor e os admins
            number.connection_state = state
            db.add(number)
            db.commit()
        return []

    if event == "messages.update":
        _apply_self_reads(db, number, data)
        _apply_acks(db, data)
        return []

    if event == "contacts.upsert":
        _apply_saved_names(db, number, data)
        return []

    if event == "call":
        for call in data if isinstance(data, list) else [data]:
            _apply_call(db, number, call)
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
    if not jid or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        return None  # status e canais não entram
    if jid.endswith("@g.us"):
        _handle_group_message(db, number, item)
        return None  # grupo: sem cópia de mídia (grupo movimentado encheria o armazenamento)

    if _apply_edit_or_delete(db, number, item) or _apply_reaction(db, number, item):
        return None

    phone = _phone_from_key(key, item, db, number)
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
    seconds = _audio_seconds(item.get("message") or {}) if media_type == "audio" else 0
    call = calls.parse_log(item.get("message") or {})  # 📞 registro de ligação (inclusive a feita no celular)
    if call is not None:
        body = calls.text("out" if key.get("fromMe") else "in", call)
    if key.get("fromMe"):
        message = _with_quote(db, _set_seconds(db, record_outbound_from_phone(db, number, phone, wa_message_id, body, media_id, media_type, secret=secret), seconds), context_info)
        if call is not None and message is not None:
            calls.mark(db, message, "out", call["outcome"], call["seconds"])
        return message

    message = _with_quote(db, _set_seconds(db, ingest_inbound(
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
    ), seconds), context_info)
    if call is not None and message is not None:
        calls.mark(db, message, "in", call["outcome"], call["seconds"])
    return message


CALL_STATUS = {"offer": "tocando…", "ringing": "tocando…", "accept": "atendida", "reject": "recusada",
               "timeout": "não atendida", "terminate": "encerrada"}


def _apply_call(db: Session, number: WhatsAppNumber, call: dict) -> None:
    """📞 Ligação que o cliente fez pro WhatsApp do vendedor (evento CALL do Evolution): vira um
    registro na conversa, atualizado a cada mudança (tocando → atendida/não atendida). Só de
    quem já é lead. As ligações feitas/atendidas no celular chegam como registro (services/calls.py)."""
    from app.services.lead_match import find_lead_by_phone

    call_id, jid = str(call.get("id") or ""), str(call.get("from") or call.get("chatId") or "")
    if not call_id or not jid or call.get("isGroup") or jid.endswith("@g.us"):
        return
    phone = _phone_from_key({"remoteJid": jid}, call, db, number)
    lead = find_lead_by_phone(db, number.tenant_id, phone) if phone else None
    if lead is None or lead.is_group:
        return
    conversation = (db.query(Conversation).filter(Conversation.lead_id == lead.id, Conversation.whatsapp_number_id == number.id).first()
                    or lead.active_conversation)
    if conversation is None:
        return
    status = CALL_STATUS.get(str(call.get("status") or "").lower(), "")  # tocando/atendida/não atendida...
    kind = "📹 Chamada de vídeo" if call.get("isVideo") else "📞 Ligação"
    body = f"{kind} do cliente pelo WhatsApp {number.label}" + (f" · {status}" if status else "")
    wa_id = f"call:{call_id}"[:120]
    record = db.query(Message).filter(Message.wa_message_id == wa_id).first()
    if record is None:
        record = Message(conversation_id=conversation.id, direction="note", wa_message_id=wa_id, body=body)
    elif status in ("tocando…", "encerrada") and "·" in record.body and "tocando" not in record.body:
        return  # já sabe se foi atendida; "encerrada" não diz mais nada
    record.body = body
    db.add(record)
    calls.mark(db, record, "in", calls.EVENT_OUTCOMES.get(str(call.get("status") or "").lower(), record.call_outcome))


def _audio_seconds(message: dict) -> int:
    """Duração que o WhatsApp manda junto do áudio (audioMessage.seconds)."""
    try:
        return max(0, int((message.get("audioMessage") or {}).get("seconds") or 0))
    except (TypeError, ValueError):
        return 0


def _set_seconds(db: Session, message, seconds: int):
    if message is not None and seconds and hasattr(message, "media_seconds") and not message.media_seconds:
        message.media_seconds = seconds
        db.commit()
    return message


def _with_quote(db: Session, message, context_info: dict):
    """Mensagem que responde outra (o "responder" do WhatsApp): guarda qual e um trecho dela."""
    quoted_id = (context_info or {}).get("stanzaId") or ""
    if message is not None and quoted_id and not message.quote_wa_id:
        text, media_type, _ = _parse_content(context_info.get("quotedMessage") or {})
        if not text:
            original = db.query(Message.body).filter(Message.wa_message_id == quoted_id).first()
            text = original[0] if original else ""
        message.quote_wa_id, message.quote_text = quoted_id[:120], (text or "Mensagem")[:300]
        db.commit()
    return message


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
                msg.body = UNSUPPORTED_TEXT
                db.add(msg)
            fixed += 1
        db.commit()
        return fixed
    finally:
        db.close()


def _handle_group_message(db: Session, number: WhatsAppNumber, item: dict) -> None:
    """Mensagem de grupo: vai pro Inbox do dono do número, com o nome de quem falou.
    Grupo não é lead: não passa por atribuição, rodízio, Pipeline nem métricas."""
    if _apply_edit_or_delete(db, number, item):
        return
    key = item.get("key") or {}
    jid, wa_message_id = key.get("remoteJid", ""), key.get("id", "")
    body, media_type, _ = _parse_content(item.get("message") or {})
    if (not body and not media_type) or message_exists(db, wa_message_id):
        return

    group = (
        db.query(Lead)
        .filter(Lead.tenant_id == number.tenant_id, Lead.phone == jid, Lead.is_group.is_(True))
        .first()
    )
    if group is None:
        group = Lead(
            tenant_id=number.tenant_id,
            name=evolution_client.group_subject(number.evolution_instance, jid) or "Grupo de WhatsApp",
            phone=jid,
            source="whatsapp_group",
            is_group=True,
            whatsapp_number_id=number.id,
            assigned_user_id=number.owner_user_id,
        )
        db.add(group)
        db.commit()
        db.refresh(group)

    from_me = bool(key.get("fromMe"))
    sender = "" if from_me else (item.get("pushName") or (key.get("participant") or "").split("@")[0])
    conversation = (
        db.query(Conversation)
        .filter(Conversation.lead_id == group.id, Conversation.whatsapp_number_id == number.id)
        .first()
    )
    if conversation is None:
        conversation = Conversation(tenant_id=number.tenant_id, lead_id=group.id, whatsapp_number_id=number.id)
        db.add(conversation)
        db.flush()
    now = datetime.datetime.utcnow()
    if from_me:
        group.last_outbound_at = now
    else:
        group.last_inbound_at = now
    conversation.last_message_at = now
    conversation.last_preview = (f"{sender}: {body}" if sender else body)[:200]
    db.add(Message(
        conversation_id=conversation.id,
        direction="out" if from_me else "in",
        wa_message_id=wa_message_id,
        body=body,
        media_id=wa_message_id if media_type else "",
        media_type=media_type,
        sender_name=sender[:120],
        secret=secret_b64(item.get("message") or {}),
        ack=1 if from_me else 0,
    ))
    db.commit()
