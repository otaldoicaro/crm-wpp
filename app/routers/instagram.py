"""📸 Direct do Instagram: webhook único da Meta (todos os clientes) + conectar/desconectar a conta.

No Meta for Developers → app da agência → Instagram → Webhooks:
- URL de retorno: https://<domínio do CRM>/webhooks/instagram
- Verificar token: o que aparece em ⚙ Configurações → Integrações → 📸 Instagram (instagram.verify_token)
- Campo: messages
A Meta diz de qual conta é a mensagem (entry.id); a gente acha a conexão (e o cliente) por ele.
"""

from __future__ import annotations

import datetime
import logging
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.deps import current_tenant, current_user_required
from app.models import Tenant, User, WhatsAppNumber
from app.services import instagram
from app.services.inbound import ingest_inbound, record_outbound_from_phone
from app.services.lead_match import find_lead_by_phone
from app.services.media_store import backup_message_media

router = APIRouter()
logger = logging.getLogger("instagram_webhook")

PLACEHOLDER = {
    "image": ("📷 Imagem", "image"),
    "video": ("🎥 Vídeo", "video"),
    "audio": ("🎤 Mensagem de áudio", "audio"),
    "file": ("📄 Arquivo", "document"),
    "share": ("🔗 Compartilhou uma publicação", ""),
    "ig_reel": ("🎬 Compartilhou um reels", ""),
    "reel": ("🎬 Compartilhou um reels", ""),
    "story_mention": ("📣 Mencionou você no story", ""),
    "animated_image": ("🖼️ GIF", "image"),
    "sticker": ("🖼️ Figurinha", "image"),
}


@router.get("/webhooks/instagram")
def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == instagram.verify_token():
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


@router.post("/webhooks/instagram")
async def receive(request: Request, background: BackgroundTasks):
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    for message_id in await run_in_threadpool(_process, payload):  # banco fora do laço principal
        background.add_task(backup_message_media, message_id)
    return JSONResponse({"ok": True})


def _process(payload: dict) -> list:
    db = SessionLocal()
    try:
        media = []
        for entry in payload.get("entry", []) if isinstance(payload, dict) else []:
            account_id = str(entry.get("id", ""))
            number = (db.query(WhatsAppNumber)
                      .filter(WhatsAppNumber.provider == instagram.PROVIDER, WhatsAppNumber.is_active.is_(True),
                              WhatsAppNumber.waba_phone_number_id == account_id).first()) if account_id else None
            if number is None:
                logger.info("instagram: mensagem pra conta não conectada %s", account_id)
                continue
            for event in entry.get("messaging", []):
                message = _handle(db, number, account_id, event)
                if message is not None and message.media_url:
                    media.append(message.id)
        return media
    finally:
        db.close()


def _content(msg: dict) -> tuple:
    """(texto, tipo de mídia, link do arquivo)."""
    text = (msg.get("text") or "").strip()
    media_type, url, labels = "", "", []
    for att in msg.get("attachments") or []:
        label, kind = PLACEHOLDER.get(att.get("type", ""), ("📎 Anexo", ""))
        labels.append(label)
        link = ((att.get("payload") or {}).get("url") or "")
        if kind and not media_type and link and len(link) <= 500:
            media_type, url = kind, link
    story = (msg.get("reply_to") or {}).get("story")
    prefix = "↩️ Respondeu ao seu story" if story else ""
    parts = [p for p in (prefix, " · ".join(labels), text) if p]
    return (" — ".join(parts) or "[mensagem do Instagram]"), media_type, url


def _handle(db: Session, number: WhatsAppNumber, account_id: str, event: dict):
    msg = event.get("message")
    if not isinstance(msg, dict) or msg.get("is_deleted") or msg.get("is_unsupported"):
        return None  # reação, "visto", mensagem apagada...
    mid = str(msg.get("mid") or "")
    sender = str((event.get("sender") or {}).get("id") or "")
    recipient = str((event.get("recipient") or {}).get("id") or "")
    body, media_type, url = _content(msg)
    when = None
    if event.get("timestamp"):
        try:
            when = datetime.datetime.utcfromtimestamp(int(event["timestamp"]) / 1000)
        except (TypeError, ValueError, OverflowError):
            when = None

    if msg.get("is_echo") or sender == account_id:
        # o time respondeu pelo app do Instagram (ou é o eco do que o CRM mandou: não duplica)
        message = record_outbound_from_phone(db, number, instagram.contact_key(recipient), mid, body,
                                             media_id=mid if media_type else "", media_type=media_type)
    else:
        if not sender:
            return None
        contact = instagram.contact_key(sender)
        name = ""
        if find_lead_by_phone(db, number.tenant_id, contact) is None:
            name = instagram.display_name(instagram.profile(number, sender)) or "Contato do Instagram"
        message = ingest_inbound(db, number, from_phone=contact, wa_message_id=mid, body=body,
                                 media_id=mid if media_type else "", media_type=media_type, profile_name=name)
        if message is not None:
            lead = message.conversation.lead
            if lead.source == "whatsapp" and instagram.is_ig_contact(lead.phone):
                lead.source = "instagram"
    if message is not None and url:
        message.media_url = url
    if message is not None:
        db.commit()
    return message


# ---------- ⚙ Configurações → Integrações → 📸 Instagram ----------

def _back(msg: str) -> RedirectResponse:
    return RedirectResponse(url="/configuracoes?aba=integracoes&ig=" + quote(msg[:500]) + "#instagram", status_code=302)


@router.post("/configuracoes/instagram")
def connect(
    token: str = Form(""),
    conta_id: str = Form(""),
    acao: str = Form("conectar"),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin" or user.read_only:
        raise HTTPException(status_code=403)
    current = [n for n in tenant.whatsapp_numbers if n.provider == instagram.PROVIDER and n.is_active]
    if acao == "desconectar":
        for n in current:
            n.is_active = False
            n.access_token = ""
            n.connection_state = "close"
        db.commit()
        return _back("Instagram desconectado. As conversas continuam no CRM; mensagens novas do Direct não entram mais.")

    token = token.strip()
    if not token:
        return _back("⚠️ Cole o token gerado no Meta for Developers.")
    conta_id = "".join(ch for ch in conta_id if ch.isdigit())[:30]
    if instagram.is_fb_token(token) and not conta_id:
        return _back("⚠️ Esse token é do Facebook (login unificado): preencha também o ID da conta do Instagram "
                     "(o número embaixo do nome da conta, em Gerar tokens de acesso).")
    try:
        info = instagram.account(token, conta_id)
        account_id = str(info.get("user_id") or info.get("id") or "")
        if not account_id:
            raise instagram.InstagramError("a Meta não devolveu o id da conta")
        try:
            instagram.subscribe(token, account_id)
        except instagram.InstagramError as exc:
            if not instagram.is_fb_token(token):
                raise
            logger.warning("instagram: não ligou o webhook pela API (%s); ligue em Assinatura do webhook no Meta", exc)
    except Exception as exc:  # noqa: BLE001 — mensagem pro admin, detalhe no log
        logger.warning("instagram: falha ao conectar: %s", exc)
        return _back(f"⚠️ Não conectou: {exc}. Confira se o token é da conta do Instagram certa e tem as permissões de mensagens.")

    taken = (db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == instagram.PROVIDER, WhatsAppNumber.is_active.is_(True),
                                             WhatsAppNumber.waba_phone_number_id == account_id,
                                             WhatsAppNumber.tenant_id != tenant.id).first())
    if taken is not None:
        return _back("⚠️ Essa conta do Instagram já está conectada em outro cliente do CRM.")
    number = next((n for n in tenant.whatsapp_numbers if n.provider == instagram.PROVIDER and n.waba_phone_number_id == account_id), None)
    if number is None:
        number = WhatsAppNumber(tenant_id=tenant.id, provider=instagram.PROVIDER, phone_number="", waba_phone_number_id=account_id)
        db.add(number)
    for other in current:
        if other is not number:
            other.is_active = False  # uma conta do Instagram por cliente
    username = info.get("username") or ""
    number.label = f"📸 Instagram @{username}" if username else "📸 Instagram"
    number.access_token = token
    number.token_renewed_at = datetime.datetime.utcnow()  # token de 60 dias: o CRM renova sozinho a cada 20
    number.connection_state = "open"
    number.is_active = True
    db.commit()
    return _back(f"✅ Instagram {('@' + username) if username else ''} conectado. As próximas mensagens do Direct entram no Inbox.")
