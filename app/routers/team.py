"""Equipe do cliente.

- /equipe (admin): aprova pedidos de acesso (feitos em /solicitar-acesso),
  muda o nível (vendedor/admin), pausa/volta alguém no rodízio, desativa quem
  saiu da empresa e tem o link de convite (atalho que dispensa aprovação).
- /convite/{token} (público): o vendedor abre o link, cria o próprio login e
  cai direto na tela "Meu WhatsApp" pra conectar o celular. O token identifica
  o cliente, então funciona mesmo sem subdomínio próprio.
"""

import datetime
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import create_session_token, hash_password
from app.config import PUBLIC_BASE_URL, SESSION_COOKIE_NAME
from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Conversation, Lead, Message, PipelineStage, Tenant, User, WhatsAppNumber
from app.services import evolution_client
from app.services.distribution import assign_lead
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
    pending = [u for u in everyone if u.pending_approval and not u.removed_at]
    removed = [u for u in everyone if u.removed_at]
    users = sorted(
        (u for u in everyone if not u.pending_approval and not u.removed_at),
        key=lambda u: (u.role != "admin", not u.is_active, u.name.lower()),
    )
    # mesmo nome em mais de um cadastro (ex: pediu acesso duas vezes com e-mails diferentes)
    by_name: dict = {}
    for u in everyone:
        if not u.removed_at:
            by_name.setdefault(" ".join(u.name.lower().split()), []).append(u)
    duplicates = {u.id: [o for o in group if o.id != u.id] for group in by_name.values() if len(group) > 1 for u in group}
    terminal = [s.id for s in db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id) if s.is_won or s.is_lost]
    open_leads = dict(
        db.query(Lead.assigned_user_id, func.count(Lead.id))
        .filter(Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None), Lead.stage_id.notin_(terminal))
        .group_by(Lead.assigned_user_id)
        .all()
    )
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
            "removed": removed,
            "duplicates": duplicates,
            "open_leads": open_leads,
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


@router.post("/equipe/niveis")
async def save_roles(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Salva os níveis escolhidos na tabela (campo role_<id> = agent | admin).
    O próprio nível não muda por aqui (evita o último admin se rebaixar)."""
    _require_admin(user)
    form = await request.form()
    for member in db.query(User).filter(User.tenant_id == tenant.id, User.id != user.id):
        role = form.get(f"role_{member.id}")
        if role in ("agent", "admin") and role != member.role:
            member.role = role
            db.add(member)
    db.commit()
    return RedirectResponse(url="/equipe?salvo=1", status_code=302)


@router.post("/equipe/{user_id}/{action}")
def team_action(
    user_id: str,
    action: str,
    repassar_leads: str = Form(""),
    remover_whatsapp: str = Form(""),
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
    elif action == "remover" and member.id != user.id:
        _remove_member(db, tenant, member, bool(repassar_leads), bool(remover_whatsapp))
        return RedirectResponse(url="/equipe?salvo=1", status_code=302)
    elif action == "excluir" and member.removed_at:
        _purge_member(db, member)
        return RedirectResponse(url="/equipe?salvo=1", status_code=302)
    elif action == "restaurar" and member.removed_at:
        member.removed_at = None
        member.is_active = True
        member.accepting_leads = True
    db.add(member)
    db.commit()
    return RedirectResponse(url="/equipe", status_code=302)


def _remove_member(db: Session, tenant: Tenant, member: User, reassign: bool, drop_whatsapp: bool) -> None:
    member.removed_at = datetime.datetime.utcnow()
    member.is_active = False  # não entra mais e sai do rodízio
    member.accepting_leads = False
    member.pending_approval = False
    db.add(member)
    db.commit()

    if reassign:
        terminal = {s.id for s in db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id) if s.is_won or s.is_lost}
        for lead in db.query(Lead).filter(
            Lead.tenant_id == tenant.id, Lead.assigned_user_id == member.id, Lead.deleted_at.is_(None)
        ):
            if lead.stage_id not in terminal:
                assign_lead(db, lead)  # rodízio entre quem continua ativo

    if drop_whatsapp:
        for number in db.query(WhatsAppNumber).filter(
            WhatsAppNumber.owner_user_id == member.id, WhatsAppNumber.is_active.is_(True)
        ):
            if number.provider == "evolution":
                for call in (evolution_client.logout, evolution_client.delete_instance):
                    try:
                        call(number.evolution_instance)
                    except evolution_client.EvolutionError:
                        pass
            number.is_active = False
            number.connection_state = "close"
            db.add(number)
        db.commit()


def _purge_member(db: Session, member: User) -> None:
    """Exclui de vez um usuário já removido. O que estava no nome dele continua no
    CRM, só que sem dono (leads ficam "sem vendedor", mensagens sem autor)."""
    uid = member.id
    db.query(Lead).filter(Lead.assigned_user_id == uid).update({Lead.assigned_user_id: None}, synchronize_session=False)
    db.query(Lead).filter(Lead.deleted_by_user_id == uid).update({Lead.deleted_by_user_id: None}, synchronize_session=False)
    db.query(Conversation).filter(Conversation.assigned_user_id == uid).update(
        {Conversation.assigned_user_id: None}, synchronize_session=False
    )
    db.query(Message).filter(Message.sender_user_id == uid).update({Message.sender_user_id: None}, synchronize_session=False)
    db.query(WhatsAppNumber).filter(WhatsAppNumber.owner_user_id == uid).update(
        {WhatsAppNumber.owner_user_id: None}, synchronize_session=False
    )
    db.delete(member)
    db.commit()


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
    password2: str = Form(""),
    db: Session = Depends(get_db),
):
    tenant = _tenant_by_invite(db, token)
    email = "".join(email.lower().split())

    def error(msg: str):
        return templates.TemplateResponse(
            request, "convite.html", {"tenant": tenant, "token": token, "error": msg}, status_code=400
        )

    if len(password) < 6:
        return error("A senha precisa ter pelo menos 6 caracteres.")
    if password != password2:
        return error("As duas senhas não são iguais. Digite de novo.")
    member = db.query(User).filter(User.tenant_id == tenant.id, User.email == email).first()
    if member and not member.removed_at:
        return error("Já existe um login com esse e-mail. Use a tela de entrar.")
    if member is None:
        member = User(tenant_id=tenant.id, email=email)
    member.name, member.password_hash, member.role = name.strip(), hash_password(password), "agent"
    member.removed_at, member.is_active, member.pending_approval, member.accepting_leads = None, True, False, True
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
