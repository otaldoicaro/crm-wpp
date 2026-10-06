from html import escape

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth import create_session_token, verify_password
from app.config import SECRET_KEY, SESSION_COOKIE_NAME
from app.db import get_db
from app.deps import current_tenant
from app.auth import hash_password
from app.models import Tenant, User, WhatsAppNumber
from app.services import mailer
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
    password2: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
):
    """Vendedor pede acesso; o login só funciona depois que um admin aprovar em Equipe."""
    email = "".join(email.lower().split())

    def render(error=None, sent=False, status_code=200):
        return templates.TemplateResponse(
            request, "solicitar_acesso.html", {"tenant": tenant, "error": error, "sent": sent}, status_code=status_code
        )

    if len(password) < 6:
        return render("A senha precisa ter pelo menos 6 caracteres.", status_code=400)
    if password != password2:
        return render("As duas senhas não são iguais. Digite de novo.", status_code=400)
    existing = db.query(User).filter(User.tenant_id == tenant.id, User.email == email).first()
    if existing and existing.removed_at:
        # já foi da equipe e foi removido: vira um pedido novo, que o admin aprova de novo
        existing.name, existing.password_hash = name.strip(), hash_password(password)
        existing.removed_at, existing.pending_approval, existing.is_active, existing.role = None, True, False, "agent"
        db.add(existing)
        db.commit()
        return render(sent=True)
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


# ---------- "Esqueceu a senha?" ----------
# O link vale 1 hora e uma vez só: o token carrega um pedaço do hash da senha
# atual, então depois que a senha muda ele deixa de valer.
RESET_MAX_AGE = 60 * 60
_reset_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="redefinir-senha")


def _reset_fingerprint(user: User) -> str:
    return user.password_hash[-16:]


def _user_from_reset_token(db: Session, tenant: Tenant, token: str):
    try:
        data = _reset_serializer.loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = db.get(User, data.get("u"))
    if not user or user.tenant_id != tenant.id or not user.is_active or user.removed_at:
        return None
    if data.get("h") != _reset_fingerprint(user):
        return None  # já foi usado (a senha mudou depois que o link foi gerado)
    return user


def _public_base(request: Request) -> str:
    host = request.headers.get("host", "")
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) or ".localhost" in host else "https"
    return f"{scheme}://{host}"


@router.get("/esqueci-senha", response_class=HTMLResponse)
def forgot_page(request: Request, tenant: Tenant = Depends(current_tenant)):
    return templates.TemplateResponse(
        request, "esqueci_senha.html", {"tenant": tenant, "sent": False, "email_ready": mailer.is_configured()}
    )


@router.post("/esqueci-senha", response_class=HTMLResponse)
def forgot_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
):
    user = db.query(User).filter(User.tenant_id == tenant.id, User.email == email.lower().strip()).first()
    if user and user.is_active and not user.removed_at:
        token = _reset_serializer.dumps({"u": user.id, "h": _reset_fingerprint(user)})
        link = f"{_public_base(request)}/redefinir-senha/{token}"
        text = (
            f"Olá, {user.name}!\n\n"
            f"Recebemos um pedido pra redefinir sua senha do CRM {tenant.name}.\n"
            f"Clique no link abaixo pra criar uma senha nova (vale por 1 hora):\n\n{link}\n\n"
            "Se não foi você, é só ignorar este e-mail: sua senha continua a mesma."
        )
        html = (
            f"<p>Olá, {escape(user.name)}!</p>"
            f"<p>Recebemos um pedido pra redefinir sua senha do CRM <b>{escape(tenant.name)}</b>.</p>"
            f"<p><a href='{link}' style='display:inline-block;padding:12px 22px;background:#FDB813;color:#15130F;"
            "border-radius:999px;font-weight:800;text-decoration:none'>Criar senha nova</a></p>"
            "<p style='color:#645E52;font-size:13px'>O link vale por 1 hora. Se não foi você, é só ignorar este "
            "e-mail: sua senha continua a mesma.</p>"
        )
        mailer.send_email(user.email, f"Redefinir sua senha — CRM {tenant.name}", text, html, from_name=f"CRM {tenant.name}")
    # mesma resposta exista ou não o e-mail (não revela quem tem cadastro)
    return templates.TemplateResponse(
        request, "esqueci_senha.html", {"tenant": tenant, "sent": True, "email_ready": mailer.is_configured()}
    )


@router.get("/redefinir-senha/{token}", response_class=HTMLResponse)
def reset_page(token: str, request: Request, db: Session = Depends(get_db), tenant: Tenant = Depends(current_tenant)):
    user = _user_from_reset_token(db, tenant, token)
    return templates.TemplateResponse(
        request, "redefinir_senha.html", {"tenant": tenant, "token": token, "valid": user is not None, "error": None}
    )


@router.post("/redefinir-senha/{token}", response_class=HTMLResponse)
def reset_submit(
    token: str,
    request: Request,
    password: str = Form(...),
    password2: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
):
    user = _user_from_reset_token(db, tenant, token)

    def render(error):
        return templates.TemplateResponse(
            request,
            "redefinir_senha.html",
            {"tenant": tenant, "token": token, "valid": user is not None, "error": error},
            status_code=400,
        )

    if user is None:
        return render(None)
    if len(password) < 6:
        return render("A senha precisa ter pelo menos 6 caracteres.")
    if password != password2:
        return render("As duas senhas não são iguais. Digite de novo.")
    user.password_hash = hash_password(password)
    db.add(user)
    db.commit()
    return RedirectResponse(url="/login?senha=1", status_code=302)
