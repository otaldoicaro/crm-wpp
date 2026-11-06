"""👥 Equipes de vendedores (configuradas pelo admin em Equipe, por cliente).

Vendedor numa equipe vê e responde as conversas dos colegas da mesma equipe (Inbox, conversa,
envio, mídia, ações do lead). Fora de equipe, continua vendo só as dele. Admin vê tudo.
Não entram no compartilhamento: grupos de WhatsApp e contatos 🔒 Time/Outro (conversa pessoal
de cada vendedor)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models import Lead, SellerTeam, User
from app.services.inbox_state import NOT_SALES_TAGS


def tenant_teams(db: Session, tenant_id: str) -> list:
    return db.query(SellerTeam).filter(SellerTeam.tenant_id == tenant_id).order_by(SellerTeam.name).all()


def members(db: Session, tenant_id: str, team_id: str) -> list:
    """Vendedores da equipe que estão na empresa (removidos e visitantes não contam)."""
    return (db.query(User).filter(User.tenant_id == tenant_id, User.team_id == team_id, User.removed_at.is_(None),
                                  User.pending_approval.is_(False), User.read_only.is_(False), User.role == "agent")
            .order_by(User.name).all())


def team_of(db: Session, user: User) -> Optional[SellerTeam]:
    if user is None or user.role != "agent" or not user.team_id:
        return None
    team = db.get(SellerTeam, user.team_id)
    return team if team is not None and team.tenant_id == user.tenant_id else None


def peer_ids(db: Session, user: User) -> list:
    """Colegas de equipe deste vendedor (sem ele mesmo). Admin ou fora de equipe: []."""
    team = team_of(db, user)
    if team is None:
        return []
    return [u.id for u in members(db, user.tenant_id, team.id) if u.id != user.id]


def shared_condition(peers: list):
    """Condição SQL (com Lead no FROM) dos leads dos colegas que este vendedor pode ver."""
    return and_(Lead.assigned_user_id.in_(peers), Lead.tag.notin_(NOT_SALES_TAGS), Lead.is_group.is_(False))


def shares(lead: Optional[Lead], peers: list) -> bool:
    return bool(lead is not None and peers and lead.assigned_user_id in peers and not lead.is_group
                and (lead.tag or "") not in NOT_SALES_TAGS)
