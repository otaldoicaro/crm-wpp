"""Landing-ponte e link rotativo.

/go/{tenant_id}: link ÚNICO pra usar em anúncio, botão do site, bio do
Instagram etc. Cada clique abre o WhatsApp de um vendedor diferente (rodízio).

/go/{tenant_id}/{whatsapp_number_id}: sempre o mesmo número.

Nos dois casos: recebe o clique de um anúncio do Google Ads, guarda o
gclid + UTMs, e redireciona pro WhatsApp já com a mensagem pré-preenchida
contendo o código de rastreio (ex: "Quero saber mais! #A1B2C3D4").

Uso: configure a URL final do anúncio do Google Ads para apontar aqui, ex:
https://SEU_DOMINIO/go/{tenant_id}/{whatsapp_number_id}?texto=Quero+saber+mais

O Google Ads substitui automaticamente {gclid} e as UTMs se você usar os
parâmetros de rastreamento dele (ValueTrack) na URL de template de rastreamento,
ou simplesmente ative "gclid" como parâmetro final da URL nas configurações da conta.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ClickBridge, WhatsAppNumber
from app.services.attribution import build_whatsapp_bridge_link, generate_tracking_code
from app.services.distribution import pick_number_for_click

router = APIRouter()


@router.get("/go/{tenant_id}")
def rotating_link(
    tenant_id: str,
    request: Request,
    texto: str = "Olá! Vim pelo anúncio e quero saber mais.",
    db: Session = Depends(get_db),
):
    """Link rotativo: um link só pra anúncio/site/bio, e cada clique abre o
    WhatsApp de um vendedor diferente (ver distribution.pick_number_for_click)."""
    number = pick_number_for_click(db, tenant_id)
    if not number:
        return HTMLResponse(
            "<p style='font-family:sans-serif;padding:24px'>Nenhum atendente disponível agora. Tente de novo em instantes.</p>",
            status_code=503,
        )
    return _redirect_with_tracking(db, tenant_id, number, request, texto, default_source="")


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
    return _redirect_with_tracking(db, tenant_id, number, request, texto, default_source="google")


def _redirect_with_tracking(
    db: Session, tenant_id: str, number: WhatsAppNumber, request: Request, texto: str, default_source: str
) -> RedirectResponse:
    params = request.query_params
    code = generate_tracking_code(db)

    source = params.get("utm_source", "")
    if not source:
        source = "google" if params.get("gclid") else "facebook" if params.get("fbclid") else default_source
    bridge = ClickBridge(
        tenant_id=tenant_id,
        tracking_code=code,
        gclid=params.get("gclid", ""),
        fbclid=params.get("fbclid", ""),
        utm_source=source,
        utm_medium=params.get("utm_medium", "cpc" if source else ""),
        utm_campaign=params.get("utm_campaign", ""),
        utm_content=params.get("utm_content", ""),
        utm_term=params.get("utm_term", ""),
        destination_phone=number.phone_number,
    )
    db.add(bridge)
    db.commit()

    link = build_whatsapp_bridge_link(number.phone_number, code, texto)
    return RedirectResponse(url=link, status_code=302)
