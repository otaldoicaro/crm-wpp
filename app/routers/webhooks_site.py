"""Ingestão de leads vindos de formulário de site.

O site do cliente faz um POST aqui com os campos do formulário + os UTMs/
fbclid/gclid que vinham na URL da página (o formulário precisa repassar
esses valores em campos hidden — é responsabilidade do site capturar isso
da querystring, não do CRM).

A URL inclui o tenant_id como uma espécie de chave pública do formulário:
não é secreta (fica embutida no HTML do site), mas isso é aceitável aqui —
o pior que alguém malicioso faria é criar leads falsos nesse tenant, não
acessar dados de outros tenants.
"""

from fastapi import APIRouter, Depends, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Lead, PipelineStage, Tenant
from app.services import attribution
from app.services.distribution import assign_lead

router = APIRouter()


@router.post("/webhooks/site-form/{tenant_id}")
def receive_site_lead(
    tenant_id: str,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    utm_source: str = Form(""),
    utm_medium: str = Form(""),
    utm_campaign: str = Form(""),
    utm_content: str = Form(""),
    utm_term: str = Form(""),
    gclid: str = Form(""),
    fbclid: str = Form(""),
    fbc: str = Form(""),
    fbp: str = Form(""),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        return JSONResponse({"ok": False, "error": "tenant inválido"}, status_code=404)

    first_stage = (
        db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).first()
    )

    lead = Lead(
        tenant_id=tenant.id,
        name=name,
        phone=phone,
        email=email,
        source="site_form",
        stage_id=first_stage.id if first_stage else None,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)

    attribution.attribution_from_site_form(
        db,
        lead,
        {
            "utm_source": utm_source,
            "utm_medium": utm_medium,
            "utm_campaign": utm_campaign,
            "utm_content": utm_content,
            "utm_term": utm_term,
            "gclid": gclid,
            "fbclid": fbclid,
            "fbc": fbc,
            "fbp": fbp,
        },
    )

    assign_lead(db, lead)

    return {"ok": True, "lead_id": lead.id}
