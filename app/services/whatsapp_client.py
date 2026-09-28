"""Envio de mensagens via WhatsApp Cloud API (Meta), usadas quando um
atendente responde um lead de dentro do CRM."""

import requests

from app.config import META_GRAPH_VERSION
from app.models import WhatsAppNumber


def fetch_media(number: WhatsAppNumber, media_id: str) -> tuple:
    """Busca os bytes de uma mídia (áudio/imagem/documento) recebida via
    WhatsApp. A Cloud API não dá uma URL pública fixa — precisa resolver o
    media_id pra uma URL temporária, e baixar autenticado. Retorna
    (content_bytes, mime_type) ou (None, None) em caso de erro."""
    lookup_url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{media_id}"
    headers = {"Authorization": f"Bearer {number.access_token}"}
    resp = requests.get(lookup_url, headers=headers, timeout=10)
    if not resp.ok:
        return None, None
    info = resp.json()
    media_url = info.get("url")
    mime_type = info.get("mime_type", "application/octet-stream")
    if not media_url:
        return None, None

    download = requests.get(media_url, headers=headers, timeout=20)
    if not download.ok:
        return None, None
    return download.content, mime_type


def send_text_message(number: WhatsAppNumber, to_phone_e164_digits: str, body: str) -> dict:
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{number.waba_phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {number.access_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone_e164_digits,
        "type": "text",
        "text": {"body": body},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    try:
        response_body = resp.json()
    except ValueError:
        response_body = {"raw": resp.text}
    return {"ok": resp.ok, "status_code": resp.status_code, "body": response_body}
