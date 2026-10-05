from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import create_session_token, verify_password
from app.config import SESSION_COOKIE_NAME
from app.db import get_db
from app.deps import current_tenant
from app.auth import hash_password
from app.models import Tenant, User, WhatsAppNumber
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, tenant: Tenant = Depends(current_tenant)):
    return templates.TemplateResponse(
        request, "login.html", {"tenant": tenant, "error": None}
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
):
    user = db.query(User).filter(User.tenant_id == tenant.id, User.email == email.lower().strip()).first()
    if user and user.pending_approval and verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"tenant": tenant, "error": "Seu pedido de acesso ainda está aguardando a aprovação do gestor."},
            status_code=401,
        )
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"tenant": tenant, "error": "E-mail ou senha inválidos"},
            status_code=401,
        )

    token = create_session_token(user.id, tenant.id)
    # vendedor que ainda não conectou o WhatsApp cai direto na tela de conectar
    has_number = (
        db.query(WhatsAppNumber.id)
        .filter(WhatsAppNumber.owner_user_id == user.id, WhatsAppNumber.is_active.is_(True))
        .first()
    )
    response = RedirectResponse(url="/" if user.role == "admin" or has_number else "/whatsapp", status_code=302)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/solicitar-acesso", response_class=HTMLResponse)
def request_access_page(request: Request, tenant: Tenant = Depends(current_tenant)):
    return templates.TemplateResponse(request, "solicitar_acesso.html", {"tenant": tenant, "error": None, "sent": False})


@router.post("/solicitar-acesso", response_class=HTMLResponse)
def request_access_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
):
    """Vendedor pede acesso; o login só funciona depois que um admin aprovar em Equipe."""
    email = email.lower().strip()

    def render(error=None, sent=False, status_code=200):
        return templates.TemplateResponse(
            request, "solicitar_acesso.html", {"tenant": tenant, "error": error, "sent": sent}, status_code=status_code
        )

    if len(password) < 6:
        return render("A senha precisa ter pelo menos 6 caracteres.", status_code=400)
    existing = db.query(User).filter(User.tenant_id == tenant.id, User.email == email).first()
    if existing:
        if existing.pending_approval:
            return render(sent=True)  # já pediu antes; só reforça a mensagem
        return render("Já existe um login com esse e-mail. Volte e use a tela de entrar.", status_code=400)

    db.add(
        User(
            tenant_id=tenant.id,
            name=name.strip(),
            email=email,
            password_hash=hash_password(password),
            role="agent",
            is_active=False,
            pending_approval=True,
        )
    )
    db.commit()
    return render(sent=True)
