"""📸 Direct do Instagram no CRM (API oficial da Meta, "Instagram API com login do Instagram").

A conta do Instagram do cliente fica guardada como uma "conexão" na mesma tabela dos WhatsApps
(WhatsAppNumber, provider="instagram"), pra conversa cair no Inbox e no Pipeline como as outras:
- phone_number fica VAZIO: assim o rodízio do link /go, a 1ª mensagem automática do formulário e a
  troca de número da conversa nunca usam o Instagram pra mandar mensagem de WhatsApp;
- waba_phone_number_id = id da conta profissional do Instagram (o "id" que vem no webhook);
- access_token = token de longa duração (60 dias), renovado sozinho 1x por dia (refresh_all);
- o contato vira Lead com phone = "ig:<id da pessoa no Instagram>".

Limites da Meta: só dá pra responder até 24h depois da última mensagem da pessoa (até 7 dias quando
quem responde é uma pessoa do time e a Meta aprovou o recurso "Human Agent" do app); texto até 1000
caracteres; arquivo (foto/áudio) ainda não sai pelo CRM, só pelo app do Instagram.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import threading
from typing import Optional

import requests

from app.config import SECRET_KEY

logger = logging.getLogger("instagram")

PROVIDER = "instagram"
PREFIX = "ig:"  # Lead.phone de quem veio pelo Direct
GRAPH = "https://graph.instagram.com"
VERSION = "v23.0"
TIMEOUT = 15


class InstagramError(Exception):
    pass


def is_instagram(number) -> bool:
    return number is not None and getattr(number, "provider", "") == PROVIDER


def is_ig_contact(phone: str) -> bool:
    return (phone or "").startswith(PREFIX)


def contact_key(igsid: str) -> str:
    return f"{PREFIX}{igsid}"


def verify_token() -> str:
    """Senha do webhook (Meta for Developers → Webhooks → Verificar token): tirada do SECRET_KEY do
    servidor, igual pra todos os clientes e fora do código."""
    return "crm-ig-" + hashlib.sha256(f"{SECRET_KEY}|instagram-webhook".encode()).hexdigest()[:20]


def _get(path: str, token: str, params: Optional[dict] = None) -> dict:
    resp = requests.get(f"{GRAPH}/{VERSION}/{path}", params={**(params or {}), "access_token": token}, timeout=TIMEOUT)
    data = resp.json() if resp.content else {}
    if not resp.ok:
        raise InstagramError((data.get("error") or {}).get("message") or f"HTTP {resp.status_code}")
    return data


def _post(path: str, token: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    resp = requests.post(f"{GRAPH}/{VERSION}/{path}", params=params, json=json,
                         headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    data = resp.json() if resp.content else {}
    if not resp.ok:
        raise InstagramError((data.get("error") or {}).get("message") or f"HTTP {resp.status_code}")
    return data


def account(token: str) -> dict:
    """{"user_id": id que vem no webhook, "username": "agenciajunta", "id": ...} da conta do token."""
    return _get("me", token, {"fields": "user_id,username,name"})


def subscribe(token: str) -> None:
    """Liga o aviso de mensagem nova (webhook) pra esta conta."""
    _post("me/subscribed_apps", token, params={"subscribed_fields": "messages", "access_token": token})


def friendly(exc: Exception) -> str:
    text = str(exc).lower()
    logger.warning("instagram: %s", exc)
    if "window" in text:
        return ("passou do prazo da Meta pra responder pelo CRM (24h da última mensagem do cliente; 7 dias quando o "
                "recurso \"Agente humano\" estiver aprovado no app). Responda pelo app do Instagram")
    if "token" in text or "session" in text or "oauth" in text:
        return "a conexão com o Instagram expirou. Um admin precisa conectar de novo em ⚙ Configurações → Integrações"
    return "o Instagram recusou o envio. Tente de novo; se continuar, responda pelo app do Instagram"


def _outside_window(exc: Exception) -> bool:
    text = str(exc).lower()
    return "window" in text or "outside of allowed" in text or "24 hour" in text


def send_text(number, contact: str, body: str, human: bool = False) -> tuple:
    """(ok, id da mensagem, erro).
    human=True (uma pessoa do time escreveu agora): se passou das 24h, tenta de novo com a marca
    HUMAN_AGENT, que a Meta aceita até 7 dias depois da última mensagem do cliente. Só funciona depois
    que a Meta aprovar o recurso "Human Agent" do app; antes disso a Meta recusa e o time responde pelo
    app do Instagram. Nunca usar em mensagem automática (regra da Meta)."""
    igsid = contact[len(PREFIX):] if is_ig_contact(contact) else contact
    message = {"recipient": {"id": igsid}, "message": {"text": body[:1000]}}
    try:
        data = _post("me/messages", number.access_token, json=message)
        return True, data.get("message_id", ""), ""
    except (InstagramError, requests.RequestException) as exc:
        if not (human and isinstance(exc, InstagramError) and _outside_window(exc)):
            return False, "", friendly(exc)
        logger.info("instagram: fora das 24h, tentando como agente humano")
    try:
        data = _post("me/messages", number.access_token, json={**message, "messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"})
        return True, data.get("message_id", ""), ""
    except (InstagramError, requests.RequestException) as exc:
        return False, "", friendly(InstagramError(f"window: {exc}"))


def profile(number, igsid: str) -> dict:
    """{"name", "username"} de quem mandou a mensagem (só funciona pra quem falou com a conta)."""
    try:
        return _get(igsid, number.access_token, {"fields": "name,username"})
    except (InstagramError, requests.RequestException, ValueError):
        return {}


def display_name(info: dict) -> str:
    name, user = (info.get("name") or "").strip(), (info.get("username") or "").strip()
    if name and user:
        return f"{name} (@{user})"
    return name or (f"@{user}" if user else "")


def fetch_media(url: str) -> tuple:
    if not url:
        return None, None
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        if resp.ok and resp.content:
            return resp.content, resp.headers.get("Content-Type", "application/octet-stream").split(";")[0]
    except requests.RequestException:
        pass
    return None, None


def refresh_all() -> int:
    """Renova o token das contas conectadas (vale 60 dias; renovar a cada ~20 dias mantém pra sempre)."""
    from app.db import SessionLocal
    from app.models import WhatsAppNumber

    renewed = 0
    db = SessionLocal()
    try:
        limit = datetime.datetime.utcnow() - datetime.timedelta(days=20)
        for number in db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == PROVIDER, WhatsAppNumber.is_active.is_(True)):
            if number.token_renewed_at and number.token_renewed_at > limit:
                continue
            try:
                resp = requests.get(f"{GRAPH}/refresh_access_token",
                                    params={"grant_type": "ig_refresh_token", "access_token": number.access_token}, timeout=TIMEOUT)
                data = resp.json() if resp.content else {}
                if resp.ok and data.get("access_token"):
                    number.access_token = data["access_token"]
                    number.token_renewed_at = datetime.datetime.utcnow()
                    number.connection_state = "open"
                    renewed += 1
                else:
                    if (data.get("error") or {}).get("code") == 190:  # token vencido/revogado: precisa conectar de novo
                        number.connection_state = "close"
                    logger.warning("instagram: não renovou o token de %s: %s", number.label, data)
            except requests.RequestException:
                logger.warning("instagram: sem resposta ao renovar o token de %s", number.label)
        db.commit()
    finally:
        db.close()
    return renewed


def schedule(delay_seconds: int = 300) -> None:
    def run():
        try:
            refresh_all()
        except Exception:
            logger.exception("instagram: renovação de token falhou")
        schedule(delay_seconds=24 * 60 * 60)

    timer = threading.Timer(delay_seconds, run)
    timer.daemon = True
    timer.start()
