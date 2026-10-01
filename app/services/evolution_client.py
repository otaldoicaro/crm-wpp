"""Chamadas à Evolution API (WhatsApp não-oficial, baseado no WhatsApp Web).

Cada número de WhatsApp é uma "instância" no Evolution, pareada lendo um QR
code com o celular (WhatsApp > Aparelhos conectados). Depois de pareado, o
número continua funcionando normal no app do celular E aqui no CRM.

Tudo aqui fala com a API REST do Evolution usando a apikey global
(EVOLUTION_API_KEY). As mensagens recebidas chegam por webhook em
app/routers/webhooks_evolution.py.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import requests

from app.config import EVOLUTION_API_KEY, EVOLUTION_API_URL, EVOLUTION_WEBHOOK_TOKEN, WEBHOOK_BASE_URL

logger = logging.getLogger("evolution_client")

WEBHOOK_EVENTS = ["MESSAGES_UPSERT", "CONNECTION_UPDATE"]


class EvolutionError(Exception):
    pass


def is_configured() -> bool:
    return bool(EVOLUTION_API_URL and EVOLUTION_API_KEY)


def _request(method: str, path: str, timeout: int = 15, **kwargs) -> dict:
    if not is_configured():
        raise EvolutionError("Evolution API não configurada (EVOLUTION_API_URL / EVOLUTION_API_KEY)")
    headers = {"apikey": EVOLUTION_API_KEY}
    try:
        resp = requests.request(method, f"{EVOLUTION_API_URL}{path}", headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise EvolutionError(f"sem resposta do Evolution: {exc}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    if not resp.ok:
        raise EvolutionError(f"Evolution respondeu {resp.status_code}: {body}")
    return body


def webhook_url() -> str:
    url = f"{WEBHOOK_BASE_URL}/webhooks/evolution"
    if EVOLUTION_WEBHOOK_TOKEN:
        url += f"?token={EVOLUTION_WEBHOOK_TOKEN}"
    return url


def jid_for(phone_digits: str) -> str:
    return f"{phone_digits}@s.whatsapp.net"


# ---------- instância / conexão ----------

def create_instance(instance: str) -> None:
    """Cria a instância já com o webhook apontando pro CRM. Se ela já existir
    no Evolution (ex: criada à mão antes), só atualiza o webhook."""
    try:
        _request(
            "POST",
            "/instance/create",
            json={"instanceName": instance, "integration": "WHATSAPP-BAILEYS", "qrcode": False},
        )
    except EvolutionError as exc:
        if "already in use" not in str(exc) and "403" not in str(exc):
            raise
        logger.info("instância %s já existia no Evolution, reaproveitando", instance)
    set_webhook(instance)


def set_webhook(instance: str) -> None:
    _request(
        "POST",
        f"/webhook/set/{instance}",
        json={
            "webhook": {
                "enabled": True,
                "url": webhook_url(),
                "byEvents": False,
                "base64": False,
                "events": WEBHOOK_EVENTS,
            }
        },
    )


def connect(instance: str, phone_digits: str = "") -> dict:
    """Pede um QR code novo (expira em ~40s). Se passar o número, o Evolution
    também devolve um código de pareamento de 8 letras, alternativa pro QR
    (WhatsApp > Aparelhos conectados > Conectar com número de telefone).
    Retorna {"qr_base64": "data:image/png;base64,...", "pairing_code": "..."}
    ou {} quando a instância já está conectada."""
    path = f"/instance/connect/{instance}"
    if phone_digits:
        path += f"?number={phone_digits}"
    body = _request("GET", path)
    qr = body.get("base64") or ""
    if qr and not qr.startswith("data:"):
        qr = "data:image/png;base64," + qr
    return {"qr_base64": qr, "pairing_code": body.get("pairingCode") or ""}


def connection_state(instance: str) -> str:
    """open = conectado | connecting = esperando ler o QR | close = desconectado"""
    body = _request("GET", f"/instance/connectionState/{instance}")
    return (body.get("instance") or {}).get("state", "") or body.get("state", "")


def owner_phone(instance: str) -> str:
    """Número (só dígitos) do WhatsApp pareado na instância, ou '' se ainda não pareou."""
    body = _request("GET", "/instance/fetchInstances", params={"instanceName": instance})
    items = body if isinstance(body, list) else [body]
    for item in items:
        data = item.get("instance", item)
        jid = data.get("ownerJid") or data.get("owner") or ""
        if jid:
            return jid.split("@")[0].split(":")[0]
    return ""


def logout(instance: str) -> None:
    _request("DELETE", f"/instance/logout/{instance}")


# ---------- mensagens ----------

def _message_id(body: dict) -> str:
    return (body.get("key") or {}).get("id", "")


def send_text(instance: str, phone_digits: str, text: str) -> str:
    """Envia texto e devolve o id da mensagem no WhatsApp."""
    body = _request("POST", f"/message/sendText/{instance}", json={"number": phone_digits, "text": text})
    return _message_id(body)


def send_media(
    instance: str, phone_digits: str, content: bytes, filename: str, mime_type: str, media_kind: str, caption: str = ""
) -> str:
    """media_kind: image | video | audio | document. Devolve o id da mensagem."""
    payload = {
        "number": phone_digits,
        "mediatype": "document" if media_kind == "audio" else media_kind,
        "mimetype": mime_type,
        "media": base64.b64encode(content).decode(),
        "fileName": filename,
        "caption": caption,
    }
    body = _request("POST", f"/message/sendMedia/{instance}", json=payload, timeout=60)
    return _message_id(body)


def fetch_media(instance: str, message_id: str) -> tuple[Optional[bytes], Optional[str]]:
    """Baixa a mídia (áudio/foto/vídeo/documento) de uma mensagem pelo id.
    O Evolution guarda a mensagem e decripta sob demanda — assim não precisamos
    salvar arquivo em disco (o disco do Render é apagado a cada deploy/sleep)."""
    try:
        body = _request(
            "POST",
            f"/chat/getBase64FromMediaMessage/{instance}",
            json={"message": {"key": {"id": message_id}}, "convertToMp4": False},
            timeout=60,
        )
    except EvolutionError as exc:
        logger.warning("falha ao baixar mídia %s: %s", message_id, exc)
        return None, None
    data = body.get("base64")
    if not data:
        return None, None
    return base64.b64decode(data), body.get("mimetype") or "application/octet-stream"


def profile_picture_url(instance: str, phone_digits: str) -> str:
    """URL da foto de perfil do contato ('' se não tem foto ou é privada).
    A URL é do servidor do WhatsApp e expira em alguns dias."""
    body = _request("POST", f"/chat/fetchProfilePictureUrl/{instance}", json={"number": phone_digits})
    return body.get("profilePictureUrl") or ""
