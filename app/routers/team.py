"""Equipe do cliente.

- /equipe (admin): aprova pedidos de acesso (feitos em /solicitar-acesso),
  muda o nível (vendedor/admin), pausa/volta alguém no rodízio, desativa quem
  saiu da empresa e tem o link de convite (atalho que dispensa aprovação).
- /convite/{token} (público): o vendedor abre o link, cria o próprio login e
  cai direto na tela "Meu WhatsApp" pra conectar o celular. O token identifica
  o cliente, então funciona mesmo sem subdomínio próprio.
"""

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import create_session_token, hash_password
from app.config import PUBLIC_BASE_URL, SESSION_COOKIE_NAME
from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Tenant, User, WhatsAppNumber
from app.tenancy import DEBUG
from app.templating import templates

router = APIRouter()


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin pode gerenciar a equipe")


def _ensure_invite_token(db: Session, tenant: Tenant) -> str:
    if not tenant.invite_token:
        tenant.invite_token = secrets.token_urlsafe(18)
        db.add(tenant)
        db.commit()
    return tenant.invite_token


@router.get("/equipe", response_class=HTMLResponse)
def team_page(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    everyone = db.query(User).filter(User.tenant_id == tenant.id).order_by(User.is_active.desc(), User.name).all()
    pending = [u for u in everyone if u.pending_approval]
    users = [u for u in everyone if not u.pending_approval]
    numbers_by_owner = {
        n.owner_user_id: n
        for n in db.query(WhatsAppNumber).filter(
            WhatsAppNumber.tenant_id == tenant.id,
            WhatsAppNumber.owner_user_id.isnot(None),
            WhatsAppNumber.is_active.is_(True),
        )
    }
    return templates.TemplateResponse(
        request,
        "equipe.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "equipe",
            "users": users,
            "pending": pending,
            "numbers_by_owner": numbers_by_owner,
            "invite_link": f"{PUBLIC_BASE_URL}/convite/{_ensure_invite_token(db, tenant)}",
        },
    )


@router.post("/equipe/convite/novo")
def new_invite_link(
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Gera outro link e invalida o anterior (ex: link vazou pra fora da equipe)."""
    _require_admin(user)
    tenant.invite_token = ""
    _ensure_invite_token(db, tenant)
    return RedirectResponse(url="/equipe", status_code=302)


@router.post("/equipe/{user_id}/{action}")
def team_action(
    user_id: str,
    action: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    _require_admin(user)
    member = db.get(User, user_id)
    if not member or member.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Pessoa não encontrada")
    if action == "aprovar" and member.pending_approval:
        member.pending_approval = False
        member.is_active = True
    elif action == "recusar" and member.pending_approval:
        db.delete(member)
        db.commit()
        return RedirectResponse(url="/equipe", status_code=302)
    elif action == "rodizio":
        member.accepting_leads = not member.accepting_leads
    elif action == "ativo" and member.id != user.id:
        member.is_active = not member.is_active
    elif action == "nivel" and member.id != user.id:  # ninguém tira o próprio admin (evita ficar sem nenhum)
        member.role = "agent" if member.role == "admin" else "admin"
    db.add(member)
    db.commit()
    return RedirectResponse(url="/equipe", status_code=302)


def _tenant_by_invite(db: Session, token: str) -> Tenant:
    tenant = db.query(Tenant).filter(Tenant.invite_token == token).first() if token else None
    if not tenant:
        raise HTTPException(status_code=404, detail="Link de convite inválido ou expirado. Peça um novo ao seu gestor.")
    return tenant


@router.get("/convite/{token}", response_class=HTMLResponse)
def invite_page(token: str, request: Request, db: Session = Depends(get_db)):
    tenant = _tenant_by_invite(db, token)
    return templates.TemplateResponse(request, "convite.html", {"tenant": tenant, "token": token, "error": None})


@router.post("/convite/{token}")
def invite_submit(
    token: str,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    tenant = _tenant_by_invite(db, token)
    email = email.lower().strip()

    def error(msg: str):
        return templates.TemplateResponse(
            request, "convite.html", {"tenant": tenant, "token": token, "error": msg}, status_code=400
        )

    if len(password) < 6:
        return error("A senha precisa ter pelo menos 6 caracteres.")
    if db.query(User).filter(User.tenant_id == tenant.id, User.email == email).first():
        return error("Já existe um login com esse e-mail. Use a tela de entrar.")

    member = User(
        tenant_id=tenant.id,
        name=name.strip(),
        email=email,
        password_hash=hash_password(password),
        role="agent",
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    response = RedirectResponse(url="/whatsapp", status_code=302)
    response.set_cookie(
        SESSION_COOKIE_NAME, create_session_token(member.id, tenant.id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14
    )
    if DEBUG:  # sem subdomínio próprio ainda (onrender.com): lembra de qual cliente é
        response.set_cookie("debug_tenant", tenant.subdomain, httponly=True, samesite="lax")
    return response
