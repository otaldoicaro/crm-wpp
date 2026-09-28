from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import create_session_token, verify_password
from app.config import SESSION_COOKIE_NAME
from app.db import get_db
from app.deps import current_tenant
from app.models import Tenant, User
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
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"tenant": tenant, "error": "E-mail ou senha inválidos"},
            status_code=401,
        )

    token = create_session_token(user.id, tenant.id)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
