"""Envio de eventos de conversão para a Meta Conversions API (CAPI).

Casamos o evento com o clique original por `ctwa_clid` (leads que vieram de
anúncio "Clique para WhatsApp") ou por `fbc`/`fbp`/`fbclid` (leads de site).
Sem um desses IDs, a Meta ainda aceita o evento usando dados do usuário
(telefone/e-mail com hash SHA-256), mas a correspondência fica mais fraca.

Pré-requisito: META_PIXEL_ID + META_CAPI_ACCESS_TOKEN no .env (gerados no
Gerenciador de Eventos do Business Manager, em "Conversions API").
"""

import hashlib
import time

import requests

from app.config import META_CAPI_ACCESS_TOKEN, META_GRAPH_VERSION, META_PIXEL_ID


def _sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def send_event(
    event_name: str,
    *,
    ctwa_clid: str = "",
    fbc: str = "",
    fbp: str = "",
    phone: str = "",
    email: str = "",
    action_source: str = "business_messaging",
) -> dict:
    """Envia um evento (Lead, Schedule, Purchase, ...) para o Meta CAPI.

    Retorna {"ok": bool, "status_code": int, "body": dict} — não levanta
    exceção em erro de API para que o chamador decida como tratar/retentar.
    """
    if not META_PIXEL_ID or not META_CAPI_ACCESS_TOKEN:
        return {"ok": False, "status_code": 0, "body": {"error": "META_PIXEL_ID/META_CAPI_ACCESS_TOKEN não configurados"}}

    user_data = {}
    if ctwa_clid:
        user_data["ctwa_clid"] = ctwa_clid
    if fbc:
        user_data["fbc"] = fbc
    if fbp:
        user_data["fbp"] = fbp
    if phone:
        user_data["ph"] = [_sha256(phone)]
    if email:
        user_data["em"] = [_sha256(email)]

    payload = {
        "data": [
            {
                "event_name": event_name,
                "event_time": int(time.time()),
                "action_source": action_source,
                "user_data": user_data,
            }
        ]
    }

    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{META_PIXEL_ID}/events"
    resp = requests.post(url, params={"access_token": META_CAPI_ACCESS_TOKEN}, json=payload, timeout=10)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    return {"ok": resp.ok, "status_code": resp.status_code, "body": body}
