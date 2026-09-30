"""Tela "WhatsApp" do painel: o admin do cliente adiciona um número e conecta
lendo o QR code com o celular, sem precisar de ninguém da agência. Mostra
também se algum número caiu (desconectou) pra reconectar na hora.
"""

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Tenant, User, WhatsAppNumber
from app.services import evolution_client
from app.templating import templates

router = APIRouter()


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin pode gerenciar os números de WhatsApp")


def _get_number(db: Session, tenant: Tenant, number_id: str) -> WhatsAppNumber:
    number = db.get(WhatsAppNumber, number_id)
    if not number or number.tenant_id != tenant.id or number.provider != "evolution":
        raise HTTPException(status_code=404, detail="Número não encontrado")
    return number


def _instance_name(db: Session, tenant: Tenant) -> str:
    """Nome da instância no Evolution: o subdomínio do cliente (ex: "novaviseu"),
    e "novaviseu-2", "novaviseu-3"... se ele tiver mais de um número."""
    base = re.sub(r"[^a-z0-9-]", "", tenant.subdomain.lower()) or "crm"
    taken = {name for (name,) in db.query(WhatsAppNumber.evolution_instance).all()}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


@router.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_page(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    numbers = (
        db.query(WhatsAppNumber)
        .filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.provider == "evolution")
        .order_by(WhatsAppNumber.created_at)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "whatsapp.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "whatsapp",
            "numbers": numbers,
            "configured": evolution_client.is_configured(),
            "error": request.query_params.get("error", ""),
            "auto_connect": request.query_params.get("connect", ""),
        },
    )


@router.post("/whatsapp/add")
def add_number(
    label: str = Form(...),
    phone: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    instance = _instance_name(db, tenant)
    try:
        evolution_client.create_instance(instance)
    except evolution_client.EvolutionError as exc:
        return RedirectResponse(url="/whatsapp?error=" + quote(f"Não foi possível criar o número: {exc}"), status_code=302)

    number = WhatsAppNumber(
        tenant_id=tenant.id,
        label=label.strip() or "WhatsApp",
        phone_number=re.sub(r"\D", "", phone),
        provider="evolution",
        evolution_instance=instance,
        connection_state="close",
    )
    db.add(number)
    db.commit()
    return RedirectResponse(url=f"/whatsapp?connect={number.id}", status_code=302)


@router.get("/whatsapp/{number_id}/qr")
def number_qr(
    number_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    number = _get_number(db, tenant, number_id)
    try:
        state = evolution_client.connection_state(number.evolution_instance)
        if state == "open":
            return JSONResponse(_mark_connected(db, number))
        evolution_client.set_webhook(number.evolution_instance)  # garante que as mensagens vão chegar
        qr = evolution_client.connect(number.evolution_instance, number.phone_number)
    except evolution_client.EvolutionError as exc:
        return JSONResponse({"state": "error", "error": str(exc)}, status_code=502)
    return JSONResponse({"state": "connecting", **qr})


@router.get("/whatsapp/{number_id}/status")
def number_status(
    number_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    number = _get_number(db, tenant, number_id)
    try:
        state = evolution_client.connection_state(number.evolution_instance)
    except evolution_client.EvolutionError as exc:
        return JSONResponse({"state": "error", "error": str(exc)}, status_code=502)
    if state == "open":
        return JSONResponse(_mark_connected(db, number))
    return JSONResponse({"state": state})


def _mark_connected(db: Session, number: WhatsAppNumber) -> dict:
    number.connection_state = "open"
    try:
        number.phone_number = evolution_client.owner_phone(number.evolution_instance) or number.phone_number
    except evolution_client.EvolutionError:
        pass
    db.add(number)
    db.commit()
    return {"state": "open", "phone": number.phone_number}


@router.post("/whatsapp/{number_id}/disconnect")
def disconnect_number(
    number_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    number = _get_number(db, tenant, number_id)
    try:
        evolution_client.logout(number.evolution_instance)
    except evolution_client.EvolutionError:
        pass  # já estava desconectado
    number.connection_state = "close"
    db.add(number)
    db.commit()
    return RedirectResponse(url="/whatsapp", status_code=302)
