"""Landing-ponte: recebe o clique de um anúncio do Google Ads, guarda o
gclid + UTMs, e redireciona pro WhatsApp já com a mensagem pré-preenchida
contendo o código de rastreio (ex: "Quero saber mais! #A1B2C3D4").

Uso: configure a URL final do anúncio do Google Ads para apontar aqui, ex:
https://SEU_DOMINIO/go/{tenant_id}/{whatsapp_number_id}?texto=Quero+saber+mais

O Google Ads substitui automaticamente {gclid} e as UTMs se você usar os
parâmetros de rastreamento dele (ValueTrack) na URL de template de rastreamento,
ou simplesmente ative "gclid" como parâmetro final da URL nas configurações da conta.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ClickBridge, WhatsAppNumber
from app.services.attribution import build_whatsapp_bridge_link, generate_tracking_code

router = APIRouter()


@router.get("/go/{tenant_id}/{whatsapp_number_id}")
def click_bridge(
    tenant_id: str,
    whatsapp_number_id: str,
    request: Request,
    texto: str = "Olá! Vim pelo anúncio e quero saber mais.",
    db: Session = Depends(get_db),
):
    number = (
        db.query(WhatsAppNumber)
        .filter(WhatsAppNumber.id == whatsapp_number_id, WhatsAppNumber.tenant_id == tenant_id)
        .first()
    )
    if not number:
        return RedirectResponse(url="https://wa.me/")

    params = request.query_params
    code = generate_tracking_code(db)

    bridge = ClickBridge(
        tenant_id=tenant_id,
        tracking_code=code,
        gclid=params.get("gclid", ""),
        utm_source=params.get("utm_source", "google"),
        utm_medium=params.get("utm_medium", "cpc"),
        utm_campaign=params.get("utm_campaign", ""),
        utm_content=params.get("utm_content", ""),
        utm_term=params.get("utm_term", ""),
        destination_phone=number.phone_number,
    )
    db.add(bridge)
    db.commit()

    link = build_whatsapp_bridge_link(number.phone_number, code, texto)
    return RedirectResponse(url=link, status_code=302)
