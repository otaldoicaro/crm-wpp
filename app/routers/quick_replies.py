"""Respostas rápidas: mensagens prontas da equipe. Na conversa, digitar "/" mostra a lista
(ver o script em inbox.html). Variáveis: {nome} = 1º nome do cliente, {vendedor} = quem atende."""

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import QuickReply, Tenant, User
from app.templating import templates

router = APIRouter()


def visible_replies(db: Session, tenant: Tenant, user: User) -> list:
    """As da equipe + as minhas, por atalho."""
    return (
        db.query(QuickReply)
        .filter(QuickReply.tenant_id == tenant.id, or_(QuickReply.shared.is_(True), QuickReply.created_by_user_id == user.id))
        .order_by(QuickReply.shortcut)
        .all()
    )


def replies_for_js(db: Session, tenant: Tenant, user: User) -> list:
    return [{"atalho": r.shortcut, "texto": r.body, "equipe": r.shared} for r in visible_replies(db, tenant, user)]


def _can_edit(user: User, reply: QuickReply) -> bool:
    return reply.created_by_user_id == user.id or user.role == "admin"


def _clean_shortcut(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", value.strip().lower().lstrip("/").replace(" ", "-"))[:40]


@router.get("/respostas", response_class=HTMLResponse)
def replies_page(
    request: Request,
    editar: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    replies = visible_replies(db, tenant, user)
    editing = next((r for r in replies if r.id == editar and _can_edit(user, r)), None)
    return templates.TemplateResponse(
        request,
        "respostas.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "inbox",
            "replies": replies,
            "editing": editing,
            "can_edit": lambda r: _can_edit(user, r),
            "error": request.query_params.get("erro", ""),
        },
    )


@router.post("/respostas")
def save_reply(
    shortcut: str = Form(...),
    body: str = Form(...),
    shared: str = Form(""),
    reply_id: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    shortcut = _clean_shortcut(shortcut)
    if not shortcut or not body.strip():
        return RedirectResponse(url="/respostas?erro=Preencha o atalho e a mensagem.", status_code=302)
    if reply_id:
        reply = db.get(QuickReply, reply_id)
        if not reply or reply.tenant_id != tenant.id or not _can_edit(user, reply):
            raise HTTPException(status_code=404)
    else:
        reply = QuickReply(tenant_id=tenant.id, created_by_user_id=user.id)
    reply.shortcut, reply.body, reply.shared = shortcut, body.strip(), bool(shared)
    db.add(reply)
    db.commit()
    return RedirectResponse(url="/respostas?salvo=1", status_code=302)


@router.post("/respostas/{reply_id}/apagar")
def delete_reply(
    reply_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    reply = db.get(QuickReply, reply_id)
    if reply and reply.tenant_id == tenant.id and _can_edit(user, reply):
        db.delete(reply)
        db.commit()
    return RedirectResponse(url="/respostas?salvo=1", status_code=302)
