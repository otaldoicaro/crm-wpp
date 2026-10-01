"""Motor de distribuição de leads entre atendentes.

Regra atual: round-robin por carga (atribui ao atendente ativo, aceitando
leads, com o MENOR número de leads abertos no momento). Empate é resolvido
por quem foi atribuído há mais tempo. Isso é mais justo que um round-robin
puro em turnos com volume desigual entre atendentes.

Para trocar a regra (ex: por horário de expediente, por número de origem,
por especialidade), mexa só nesta função — o resto do sistema chama
`assign_next_agent` sem saber os detalhes da regra.
"""

from __future__ import annotations

import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import Lead, PipelineStage, User, WhatsAppNumber


def assign_next_agent(db: Session, tenant_id: str) -> User | None:
    open_counts = (
        db.query(Lead.assigned_user_id, func.count(Lead.id).label("n"))
        .outerjoin(PipelineStage, PipelineStage.id == Lead.stage_id)
        .filter(Lead.tenant_id == tenant_id, Lead.assigned_user_id.isnot(None), Lead.deleted_at.is_(None))
        .filter(
            or_(
                Lead.stage_id.is_(None),
                (PipelineStage.is_won.is_(False)) & (PipelineStage.is_lost.is_(False)),
            )
        )
        .group_by(Lead.assigned_user_id)
        .all()
    )
    load_by_user = {user_id: n for user_id, n in open_counts}

    candidates = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.is_active.is_(True), User.accepting_leads.is_(True))
        .order_by(User.created_at)
        .all()
    )

    eligible = []
    for user in candidates:
        current_load = load_by_user.get(user.id, 0)
        if user.max_open_leads and current_load >= user.max_open_leads:
            continue
        eligible.append((current_load, user))

    if not eligible:
        return None

    eligible.sort(key=lambda pair: pair[0])
    return eligible[0][1]


def assign_lead(db: Session, lead: Lead, number: WhatsAppNumber | None = None) -> User | None:
    """Número com dono (o WhatsApp do vendedor): o lead é dele, porque é com o
    celular dele que o lead está conversando. Número compartilhado/central:
    rodízio por carga entre todos os vendedores."""
    owner = number.owner if number is not None and number.owner_user_id else None
    agent = owner if owner and owner.is_active else assign_next_agent(db, lead.tenant_id)
    lead.assigned_user_id = agent.id if agent else None
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return agent


def _open_leads_by_user(db: Session, tenant_id: str) -> dict:
    rows = (
        db.query(Lead.assigned_user_id, func.count(Lead.id))
        .outerjoin(PipelineStage, PipelineStage.id == Lead.stage_id)
        .filter(Lead.tenant_id == tenant_id, Lead.assigned_user_id.isnot(None), Lead.deleted_at.is_(None))
        .filter(or_(Lead.stage_id.is_(None), (PipelineStage.is_won.is_(False)) & (PipelineStage.is_lost.is_(False))))
        .group_by(Lead.assigned_user_id)
        .all()
    )
    return dict(rows)


def pick_number_for_click(db: Session, tenant_id: str) -> WhatsAppNumber | None:
    """Link rotativo (/go/{tenant}): escolhe pra qual WhatsApp de vendedor
    mandar ESTE clique. Revezamento por clique (o número que recebeu clique há
    mais tempo vai primeiro), pulando automaticamente número desconectado ou
    bloqueado e vendedor pausado/no limite de leads abertos. Assim, se um
    número cair, os próximos clientes vão pros outros sem ninguém mexer."""
    numbers = (
        db.query(WhatsAppNumber)
        .filter(WhatsAppNumber.tenant_id == tenant_id, WhatsAppNumber.is_active.is_(True))
        .all()
    )
    load = _open_leads_by_user(db, tenant_id)

    def eligible(n: WhatsAppNumber) -> bool:
        if n.provider == "evolution" and n.connection_state != "open":
            return False
        if not n.phone_number or not n.owner_user_id:
            return False
        owner = n.owner
        if not owner or not owner.is_active or not owner.accepting_leads:
            return False
        return not (owner.max_open_leads and load.get(owner.id, 0) >= owner.max_open_leads)

    candidates = [n for n in numbers if eligible(n)]
    if not candidates:
        # ninguém disponível no rodízio: manda pra qualquer número conectado
        candidates = [n for n in numbers if n.phone_number and (n.provider != "evolution" or n.connection_state == "open")]
    if not candidates:
        return None

    oldest = datetime.datetime(1970, 1, 1)
    chosen = min(candidates, key=lambda n: n.last_routed_at or oldest)
    chosen.last_routed_at = datetime.datetime.utcnow()
    db.add(chosen)
    db.commit()
    return chosen
