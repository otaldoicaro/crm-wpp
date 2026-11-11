"""Chamadas ao UAZapi (uazapiGO), o substituto do Evolution, ligado número por número.

Por que existe: o Evolution 2.3.7 (Baileys 7.0.0-rc.9) passou a descartar mensagens que não consegue
abrir ("Bad MAC"), e os números ficavam "surdos". O UAZapi roda no servidor deles; cada número é uma
"instância" com o próprio token.

No CRM um número do UAZapi é um WhatsAppNumber com provider="uazapi":
- evolution_instance = nome da instância no UAZapi (a coluna é reaproveitada);
- access_token = token da instância (fica no banco; nunca no repositório).

Autenticação: header "token" (instância) ou "admintoken" (criar/listar instâncias).
Documentação: https://docs.uazapi.com (openapi-bundled.json). As mensagens recebidas chegam por webhook
em app/routers/webhooks_uazapi.py — e o UAZapi NÃO repete uma entrega que falhou: quem cobre isso é a
conferência periódica (history_sync)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Optional

import requests

from app.config import PUBLIC_BASE_URL, SECRET_KEY, UAZAPI_ADMIN_TOKEN, UAZAPI_URL

logger = logging.getLogger("uazapi_client")

PROVIDER = "uazapi"
# o que o CRM precisa: mensagens (as do celular também), entregue/lida, conexão, ligações e o histórico
# que o WhatsApp manda quando o número é conectado
WEBHOOK_EVENTS = ["messages", "messages_update", "connection", "call", "history"]


class UazapiError(Exception):
    pass


class UazapiTimeout(UazapiError):
    """O pedido foi feito mas a resposta não chegou a tempo: não dá pra saber se a mensagem saiu."""


def is_configured() -> bool:
    return bool(UAZAPI_URL and UAZAPI_ADMIN_TOKEN)


def is_uazapi(number) -> bool:
    return number is not None and getattr(number, "provider", "") == PROVIDER


def _request(method: str, path: str, token: str = "", admin: bool = False, timeout: int = 20, **kwargs) -> dict:
    if not UAZAPI_URL:
        raise UazapiError("UAZapi não configurado (UAZAPI_URL)")
    headers = {"admintoken": UAZAPI_ADMIN_TOKEN} if admin else {"token": token}
    if not (headers.get("admintoken") or headers.get("token")):
        raise UazapiError("falta o token do UAZapi")
    try:
        resp = requests.request(method, f"{UAZAPI_URL}{path}", headers=headers, timeout=timeout, **kwargs)
    except requests.Timeout as exc:
        raise UazapiTimeout(f"o UAZapi demorou pra responder: {exc}") from exc
    except requests.RequestException as exc:
        raise UazapiError(f"sem resposta do UAZapi: {exc}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}
    if not resp.ok:
        raise UazapiError(f"UAZapi respondeu {resp.status_code}: {str(body)[:300]}")
    return body if isinstance(body, dict) else {"items": body}


# ---------- webhook ----------

def webhook_secret() -> str:
    """Segredo que vai no caminho do webhook (só quem sabe consegue postar mensagens no CRM)."""
    return hmac.new(SECRET_KEY.encode(), b"uazapi-webhook", hashlib.sha256).hexdigest()[:32]


def webhook_url() -> str:
    # o UAZapi fica FORA do nosso servidor: precisa do endereço público (https), não da rede interna
    return f"{PUBLIC_BASE_URL}/webhooks/uazapi/{webhook_secret()}"


def set_webhook(token: str) -> None:
    _request("POST", "/webhook", token=token, json={
        "enabled": True, "url": webhook_url(), "events": WEBHOOK_EVENTS,
        "excludeMessages": ["wasSentByApi"],  # o que o CRM mandou ele já registrou na hora do envio
    })


# ---------- instância / conexão ----------

def create_instance(name: str) -> str:
    """Cria a instância e devolve o token dela."""
    body = _request("POST", "/instance/create", admin=True, json={"name": name})
    token = body.get("token") or (body.get("instance") or {}).get("token") or ""
    if not token:
        raise UazapiError(f"o UAZapi não devolveu o token da instância: {str(body)[:200]}")
    return token


def connect(token: str, phone_digits: str = "") -> dict:
    """Começa a conexão: sem número devolve o QR code; com número, o código de pareamento.
    {"qrcode": "data:image/png;base64,...", "paircode": "ABCD-EFGH", "status": "..."}."""
    payload = {"phone": phone_digits} if phone_digits else {}
    try:
        body = _request("POST", "/instance/connect", token=token, json=payload, timeout=40)
    except UazapiError as exc:
        if "409" not in str(exc):  # já tem uma conexão em andamento: só lê o estado
            raise
        body = _request("GET", "/instance/status", token=token)
    inst = body.get("instance") or {}
    return {"qrcode": inst.get("qrcode") or "", "paircode": inst.get("paircode") or "", "status": inst.get("status") or ""}


def status(token: str) -> tuple:
    """(estado no jeito do CRM: open | connecting | close, número conectado só dígitos)."""
    body = _request("GET", "/instance/status", token=token)
    st = body.get("status") or {}
    inst = body.get("instance") or {}
    jid = st.get("jid") or {}
    phone = (jid.get("user") if isinstance(jid, dict) else str(jid).split("@")[0].split(":")[0]) or ""
    raw = inst.get("status") or ""
    if st.get("connected") and st.get("loggedIn"):
        state = "open"
    elif raw == "connecting":
        state = "connecting"
    else:
        state = "close"
    return state, "".join(ch for ch in phone if ch.isdigit())


def connection_state(token: str) -> str:
    return status(token)[0]


def disconnect(token: str) -> None:
    _request("POST", "/instance/disconnect", token=token)


def delete_instance(token: str) -> None:
    _request("DELETE", "/instance", token=token)


# ---------- envio ----------

def _sent_id(body: dict) -> str:
    """Id da mensagem enviada (o mesmo que vem depois nos webhooks de entregue/lida)."""
    return str(body.get("messageid") or body.get("id") or (body.get("message") or {}).get("messageid") or "")


def send_text(token: str, phone_digits: str, text: str, reply_id: str = "") -> str:
    payload = {"number": phone_digits, "text": text}
    if reply_id:
        payload["replyid"] = reply_id
    return _sent_id(_request("POST", "/send/text", token=token, json=payload, timeout=45))


def send_media(token: str, phone_digits: str, content: bytes, filename: str, mime_type: str, media_kind: str,
               caption: str = "", reply_id: str = "") -> str:
    """media_kind: image | video | audio | document | ptt (mensagem de voz)."""
    payload = {
        "number": phone_digits, "type": media_kind, "file": base64.b64encode(content).decode(),
        "mimetype": mime_type, "text": caption,
    }
    if media_kind == "document":
        payload["docName"] = filename
    if reply_id:
        payload["replyid"] = reply_id
    return _sent_id(_request("POST", "/send/media", token=token, json=payload, timeout=120))


def send_audio(token: str, phone_digits: str, content: bytes) -> str:
    """Áudio como mensagem de voz (a bolinha verde, "gravado na hora")."""
    return send_media(token, phone_digits, content, "audio.ogg", "audio/ogg", "ptt")


def edit_text(token: str, message_id: str, text: str) -> None:
    _request("POST", "/message/edit", token=token, json={"id": message_id, "text": text})


def delete_for_everyone(token: str, message_id: str) -> None:
    _request("POST", "/message/delete", token=token, json={"id": message_id})


def mark_read(token: str, message_ids: list) -> None:
    _request("POST", "/message/markread", token=token, json={"id": list(message_ids)})


# ---------- leitura ----------

def fetch_media(token: str, message_id: str) -> tuple:
    """Baixa a mídia de uma mensagem recebida: (bytes, mimetype) ou (None, None).
    O UAZapi devolve um link público que vale 2 dias; o CRM guarda a própria cópia (media_store)."""
    try:
        body = _request("POST", "/message/download", token=token, timeout=60,
                        json={"id": message_id, "return_link": True, "generate_mp3": False})
    except UazapiError as exc:
        logger.warning("uazapi: falha ao baixar mídia %s: %s", message_id, exc)
        return None, None
    url = body.get("fileURL") or ""
    if not url:
        data = body.get("base64Data")
        return (base64.b64decode(data), body.get("mimetype") or "application/octet-stream") if data else (None, None)
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("uazapi: falha ao baixar o arquivo da mídia %s: %s", message_id, exc)
        return None, None
    return resp.content, body.get("mimetype") or resp.headers.get("content-type") or "application/octet-stream"


def find_messages(token: str, chat_id: str = "", limit: int = 200, offset: int = 0) -> dict:
    """Mensagens guardadas no UAZapi (as mais novas primeiro). {"messages": [...], "hasMore", "nextOffset"}.
    Não tem filtro de data: quem chama vai paginando até passar do período."""
    payload = {"limit": limit, "offset": offset}
    if chat_id:
        payload["chatid"] = chat_id
    body = _request("POST", "/message/find", token=token, json=payload, timeout=60)
    page = body.get("pagination") or {}
    return {"messages": body.get("messages") or [], "hasMore": bool(page.get("hasMore")),
            "nextOffset": page.get("nextOffset") or offset + limit}


def whatsapp_numbers(token: str, phones: list) -> dict:
    """{número: tem WhatsApp?} — também é o teste de que a conexão funciona de verdade."""
    body = _request("POST", "/chat/check", token=token, json={"numbers": list(phones)})
    rows = body.get("items") if "items" in body else body.get("results") or []
    return {str(r.get("query")): bool(r.get("isInWhatsapp")) for r in rows if isinstance(r, dict)}


def profile_picture_url(token: str, phone_digits: str) -> str:
    try:
        return _request("POST", "/chat/avatar", token=token, json={"number": phone_digits}).get("url") or ""
    except UazapiError:
        return ""


def group_subject(token: str, group_jid: str) -> str:
    try:
        return _request("POST", "/group/info", token=token, json={"groupjid": group_jid}).get("Name") or ""
    except UazapiError:
        return ""
