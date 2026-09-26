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

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import Lead, PipelineStage, User


def assign_next_agent(db: Session, tenant_id: str) -> User | None:
    open_counts = (
        db.query(Lead.assigned_user_id, func.count(Lead.id).label("n"))
        .outerjoin(PipelineStage, PipelineStage.id == Lead.stage_id)
        .filter(Lead.tenant_id == tenant_id, Lead.assigned_user_id.isnot(None))
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


def assign_lead(db: Session, lead: Lead) -> User | None:
    agent = assign_next_agent(db, lead.tenant_id)
    lead.assigned_user_id = agent.id if agent else None
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return agent
