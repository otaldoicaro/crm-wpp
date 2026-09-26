"""Envio de mensagens via WhatsApp Cloud API (Meta), usadas quando um
atendente responde um lead de dentro do CRM."""

import requests

from app.config import META_GRAPH_VERSION
from app.models import WhatsAppNumber


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
