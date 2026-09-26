"""Upload de conversões offline (click conversions) para o Google Ads.

Casamos pelo `gclid` capturado na landing page-ponte (ver app/services/attribution.py).
Usa a REST API do Google Ads diretamente (sem a lib google-ads, que exige gRPC e é
mais pesada) — suficiente para uploadClickConversions.

Pré-requisitos (todos em .env):
- GOOGLE_ADS_DEVELOPER_TOKEN: token de desenvolvedor aprovado pela conta MCC.
- GOOGLE_ADS_CUSTOMER_ID: id da conta do cliente (sem hífens), ex: 1234567890.
- GOOGLE_ADS_CONVERSION_ACTION_ID: id da "ação de conversão" criada no Google Ads
  (Ferramentas > Conversões > nova ação "Importação" > "Clique").
- GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET / GOOGLE_ADS_REFRESH_TOKEN:
  credenciais OAuth2 (criadas no Google Cloud Console + fluxo de consentimento
  rodado uma vez para gerar o refresh token).
"""

from __future__ import annotations

import datetime

import requests

from app.config import (
    GOOGLE_ADS_CLIENT_ID,
    GOOGLE_ADS_CLIENT_SECRET,
    GOOGLE_ADS_CONVERSION_ACTION_ID,
    GOOGLE_ADS_CUSTOMER_ID,
    GOOGLE_ADS_DEVELOPER_TOKEN,
    GOOGLE_ADS_REFRESH_TOKEN,
)

API_VERSION = "v17"


def _get_access_token() -> str | None:
    if not (GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET and GOOGLE_ADS_REFRESH_TOKEN):
        return None
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_ADS_CLIENT_ID,
            "client_secret": GOOGLE_ADS_CLIENT_SECRET,
            "refresh_token": GOOGLE_ADS_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    if not resp.ok:
        return None
    return resp.json().get("access_token")


def send_click_conversion(gclid: str, *, conversion_value: float = 0.0, currency_code: str = "BRL") -> dict:
    """Envia uma click conversion (evento comercial) casada pelo gclid.

    Retorna {"ok": bool, "status_code": int, "body": dict}.
    """
    missing = [
        name
        for name, value in [
            ("GOOGLE_ADS_DEVELOPER_TOKEN", GOOGLE_ADS_DEVELOPER_TOKEN),
            ("GOOGLE_ADS_CUSTOMER_ID", GOOGLE_ADS_CUSTOMER_ID),
            ("GOOGLE_ADS_CONVERSION_ACTION_ID", GOOGLE_ADS_CONVERSION_ACTION_ID),
        ]
        if not value
    ]
    if missing:
        return {"ok": False, "status_code": 0, "body": {"error": f"faltando config: {', '.join(missing)}"}}

    access_token = _get_access_token()
    if not access_token:
        return {"ok": False, "status_code": 0, "body": {"error": "não foi possível obter access_token OAuth2"}}

    conversion_action = f"customers/{GOOGLE_ADS_CUSTOMER_ID}/conversionActions/{GOOGLE_ADS_CONVERSION_ACTION_ID}"
    conversion_datetime = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S+00:00")

    url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{GOOGLE_ADS_CUSTOMER_ID}:uploadClickConversions"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "Content-Type": "application/json",
    }
    body = {
        "conversions": [
            {
                "gclid": gclid,
                "conversionAction": conversion_action,
                "conversionDateTime": conversion_datetime,
                "conversionValue": conversion_value,
                "currencyCode": currency_code,
            }
        ],
        "partialFailure": True,
    }

    resp = requests.post(url, headers=headers, json=body, timeout=15)
    try:
        response_body = resp.json()
    except ValueError:
        response_body = {"raw": resp.text}
    return {"ok": resp.ok, "status_code": resp.status_code, "body": response_body}
