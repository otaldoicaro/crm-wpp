"""Ingestão de leads vindos de formulário (site / landing page).

Dois formatos:
- POST /webhooks/site-form/{tenant_id}: formulário HTML comum (campos name, phone,
  email, utm_*, gclid, fbclid, fbc, fbp).
- POST /webhooks/lp/{tenant_id}: JSON (a LP da Nova Viseu manda com
  Content-Type text/plain + no-cors, pra não precisar de CORS). Campos da LP:
  nome, telefone/whatsapp, email, marca, modelo, ano, pecas, qtd_pecas_encontradas,
  pagina, utm_*, origem {utm_*, gclid, fbclid}.

Nos dois casos o lead é um por pessoa: se o telefone já existe (porque a pessoa
já chamou no WhatsApp, ou já preencheu antes), completa o mesmo lead em vez de
criar outro — ver app/services/lead_match.py.

A URL inclui o tenant_id como chave pública do formulário: não é secreta (fica
no código da página), o pior uso indevido seria criar leads falsos nesse cliente.
"""

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Tenant
from app.services.lead_match import upsert_form_lead

router = APIRouter()

CORS = {"Access-Control-Allow-Origin": "*"}


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
    if not db.get(Tenant, tenant_id):
        return JSONResponse({"ok": False, "error": "tenant inválido"}, status_code=404)
    data = dict(
        name=name, phone=phone, email=email, utm_source=utm_source, utm_medium=utm_medium,
        utm_campaign=utm_campaign, utm_content=utm_content, utm_term=utm_term,
        gclid=gclid, fbclid=fbclid, fbc=fbc, fbp=fbp,
    )
    lead, created = upsert_form_lead(db, tenant_id, data, source="site_form")
    return {"ok": True, "lead_id": lead.id, "created": created}


@router.options("/webhooks/lp/{tenant_id}")
def lp_preflight(tenant_id: str):
    return JSONResponse({}, headers={**CORS, "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type"})


@router.post("/webhooks/lp/{tenant_id}")
async def receive_lp_lead(tenant_id: str, request: Request, db: Session = Depends(get_db)):
    if not db.get(Tenant, tenant_id):
        return JSONResponse({"ok": False, "error": "tenant inválido"}, status_code=404, headers=CORS)
    try:
        payload = json.loads((await request.body()).decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400, headers=CORS)

    origem = payload.get("origem") if isinstance(payload.get("origem"), dict) else {}
    pick = lambda key: str(payload.get(key) or origem.get(key) or "")  # noqa: E731
    data = {
        "name": str(payload.get("nome") or payload.get("name") or ""),
        "phone": str(payload.get("whatsapp") or payload.get("telefone") or payload.get("phone") or ""),
        "email": str(payload.get("email") or ""),
        **{k: pick(k) for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid", "fbc", "fbp")},
        **{k: payload.get(k) for k in ("marca", "modelo", "ano", "pecas", "qtd_pecas_encontradas", "mensagem", "pagina")},
    }
    if not data["phone"] and not data["email"]:
        return JSONResponse({"ok": False, "error": "sem telefone nem e-mail"}, status_code=400, headers=CORS)
    lead, created = upsert_form_lead(db, tenant_id, data, source="site_form")
    return JSONResponse({"ok": True, "lead_id": lead.id, "created": created}, headers=CORS)
