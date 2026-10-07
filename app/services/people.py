"""Quem aparece nas listas de equipe (filtros de vendedor, dono de número etc.)."""

from __future__ import annotations

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.models import User


def team_members(db: Session, tenant_id: str, only_active: bool = False) -> list:
    """Equipe atual: sem pedidos pendentes e sem removidos; admins primeiro, depois por nome."""
    query = db.query(User).filter(
        User.tenant_id == tenant_id, User.pending_approval.is_(False), User.removed_at.is_(None)
    )
    if only_active:
        query = query.filter(User.is_active.is_(True))
    return query.order_by(case((User.role == "admin", 0), else_=1), User.name).all()


def removed_user_ids(db: Session, tenant_id: str) -> list:
    return [uid for (uid,) in db.query(User.id).filter(User.tenant_id == tenant_id, User.removed_at.isnot(None))]


def admins_out_of_rotation(db: Session) -> int:
    """Admin fica fora do rodízio por padrão: tira quem está no rodízio sem ter sido
    confirmado na tela Equipe (roda no start; quem for ligado e confirmado fica)."""
    n = (
        db.query(User)
        .filter(User.role == "admin", User.accepting_leads.is_(True), User.rodizio_confirmed_at.is_(None))
        .update({User.accepting_leads: False}, synchronize_session=False)
    )
    db.commit()
    return n
