"""👤 Meu login (/meu-perfil): cada pessoa muda o próprio nome, o e-mail com que entra e a senha.
Trocar e-mail ou senha pede a senha atual (alguém que achou o computador logado não troca o acesso)."""

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_password
from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Tenant, User
from app.templating import templates

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 6  # o mesmo do convite e do "esqueci a senha"


def _render(request: Request, tenant: Tenant, user: User, error: str = "", saved: str = "", form: dict = None, status_code: int = 200):
    return templates.TemplateResponse(request, "meu_perfil.html", {
        "tenant": tenant, "user": user, "active_nav": "perfil", "error": error, "saved": saved,
        "form": form or {"nome": user.name, "email": user.email}, "min_password": MIN_PASSWORD,
    }, status_code=status_code)


@router.get("/meu-perfil", response_class=HTMLResponse)
def profile_page(
    request: Request,
    salvo: str = "",
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    return _render(request, tenant, user, saved=salvo)


@router.post("/meu-perfil", response_class=HTMLResponse)
def profile_save(
    request: Request,
    nome: str = Form(""),
    email: str = Form(""),
    senha_atual: str = Form(""),
    senha_nova: str = Form(""),
    senha_nova2: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    name = " ".join(nome.split())[:120]
    new_email = "".join(email.lower().split())
    form = {"nome": nome, "email": email}

    def error(msg: str):
        return _render(request, tenant, user, error=msg, form=form, status_code=400)

    if not name:
        return error("Escreva o seu nome.")
    if not EMAIL_RE.match(new_email):
        return error("Esse e-mail não parece certo. Confira e tente de novo.")
    email_changed = new_email != user.email
    password_changed = bool(senha_nova or senha_nova2)
    if email_changed or password_changed:
        if not senha_atual or not verify_password(senha_atual, user.password_hash):
            return error("Pra trocar o e-mail ou a senha, digite a sua senha atual (a de hoje) no campo “Senha atual”.")
    if email_changed and db.query(User.id).filter(
        User.tenant_id == tenant.id, User.email == new_email, User.id != user.id
    ).first():
        return error("Já existe outra pessoa com esse e-mail neste CRM.")
    if password_changed:
        if len(senha_nova) < MIN_PASSWORD:
            return error(f"A senha nova precisa ter pelo menos {MIN_PASSWORD} caracteres.")
        if senha_nova != senha_nova2:
            return error("As duas senhas novas não são iguais. Digite de novo.")
        user.password_hash = hash_password(senha_nova)
    user.name = name
    user.email = new_email
    db.add(user)
    db.commit()
    what = [w for w, changed in (("senha", password_changed), ("email", email_changed)) if changed]
    return RedirectResponse(url="/meu-perfil?salvo=" + ("-".join(what) or "1"), status_code=302)
