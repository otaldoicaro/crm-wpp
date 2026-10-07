"""Tela "WhatsApp" do painel, conexão por QR code sem ninguém da agência:
- vendedor ("Meu WhatsApp"): conecta o WhatsApp do próprio celular, que vira
  o número dele no rodízio do link rotativo; só enxerga o próprio número.
- admin: vê todos os números, adiciona, escolhe o vendedor dono de cada um e
  pega o link rotativo pra usar nos anúncios/site.
Mostra também se algum número caiu (desconectou) pra reconectar na hora.
"""

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Tenant, User, WhatsAppNumber
from app.config import PUBLIC_BASE_URL
from app.services import evolution_client
from app.services.people import team_members
from app.templating import templates

router = APIRouter()


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin pode gerenciar os números de WhatsApp")


def _get_number(db: Session, tenant: Tenant, user: User, number_id: str) -> WhatsAppNumber:
    number = db.get(WhatsAppNumber, number_id)
    if not number or number.tenant_id != tenant.id or number.provider != "evolution":
        raise HTTPException(status_code=404, detail="Número não encontrado")
    if user.role != "admin" and number.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="Número não encontrado")
    return number


def _users(db: Session, tenant: Tenant) -> list:
    return team_members(db, tenant.id, only_active=True)


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
    query = db.query(WhatsAppNumber).filter(
        WhatsAppNumber.tenant_id == tenant.id,
        WhatsAppNumber.provider == "evolution",
        WhatsAppNumber.is_active.is_(True),
    )
    if user.role != "admin":
        query = query.filter(WhatsAppNumber.owner_user_id == user.id)
    numbers = query.order_by(WhatsAppNumber.created_at).all()
    return templates.TemplateResponse(
        request,
        "whatsapp.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "whatsapp",
            "numbers": numbers,
            "users": _users(db, tenant) if user.role == "admin" else [],
            "rotating_link": f"{PUBLIC_BASE_URL}/go/{tenant.id}",
            "configured": evolution_client.is_configured(),
            "error": request.query_params.get("error", ""),
            "auto_connect": request.query_params.get("connect", ""),
        },
    )


@router.post("/whatsapp/add")
def add_number(
    label: str = Form(""),
    phone: str = Form(""),
    owner_user_id: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        # vendedor só conecta o próprio WhatsApp, e um só
        existing = (
            db.query(WhatsAppNumber)
            .filter(
                WhatsAppNumber.tenant_id == tenant.id,
                WhatsAppNumber.owner_user_id == user.id,
                WhatsAppNumber.is_active.is_(True),
            )
            .first()
        )
        if existing:
            return RedirectResponse(url=f"/whatsapp?connect={existing.id}", status_code=302)
        owner_user_id, label = user.id, label or f"WhatsApp de {user.name}"
    elif owner_user_id and not any(u.id == owner_user_id for u in _users(db, tenant)):
        owner_user_id = ""

    instance = _instance_name(db, tenant)
    try:
        evolution_client.create_instance(instance)
    except evolution_client.EvolutionError as exc:
        return RedirectResponse(url="/whatsapp?error=" + quote(f"Não foi possível criar o número: {exc}"), status_code=302)

    number = WhatsAppNumber(
        tenant_id=tenant.id,
        label=label.strip() or "WhatsApp",
        phone_number=re.sub(r"\D", "", phone),
        owner_user_id=owner_user_id or None,
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
    number = _get_number(db, tenant, user, number_id)
    try:
        state = evolution_client.connection_state(number.evolution_instance)
        if state == "open":
            return JSONResponse(_mark_connected(db, number))
        try:
            evolution_client.set_webhook(number.evolution_instance)  # garante que as mensagens vão chegar
        except evolution_client.EvolutionError as exc:
            if "does not exist" not in str(exc):
                raise
            # o Evolution apagou a instância (ex: ficou muito tempo sem ler o QR): cria de novo
            evolution_client.create_instance(number.evolution_instance)  # já com o webhook
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
    number = _get_number(db, tenant, user, number_id)
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
    number = _get_number(db, tenant, user, number_id)
    try:
        evolution_client.logout(number.evolution_instance)
    except evolution_client.EvolutionError:
        pass  # já estava desconectado
    number.connection_state = "close"
    db.add(number)
    db.commit()
    return RedirectResponse(url="/whatsapp", status_code=302)


@router.post("/whatsapp/{number_id}/owner")
def set_owner(
    number_id: str,
    owner_user_id: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    number = _get_number(db, tenant, user, number_id)
    valid = {u.id for u in _users(db, tenant)}
    number.owner_user_id = owner_user_id if owner_user_id in valid else None
    db.add(number)
    db.commit()
    return RedirectResponse(url="/whatsapp", status_code=302)


@router.post("/whatsapp/{number_id}/remover")
def remove_number(
    number_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Tira o número do CRM de vez (ex: número de teste, vendedor que saiu, número
    bloqueado). Desconecta e apaga no Evolution; as conversas antigas continuam no CRM."""
    _require_admin(user)
    number = _get_number(db, tenant, user, number_id)
    for action in (evolution_client.logout, evolution_client.delete_instance):
        try:
            action(number.evolution_instance)
        except evolution_client.EvolutionError:
            pass  # já desconectado/apagado
    number.is_active = False
    number.connection_state = "close"
    db.add(number)
    db.commit()
    return RedirectResponse(url="/whatsapp?salvo=1", status_code=302)
