"""Tempo de atendimento: quanto demoramos pra responder o lead pela 1ª vez e há quanto
tempo não falamos com ele. Atualizado a cada mensagem (inbound.py / Inbox)."""

from __future__ import annotations

import datetime
import logging
import statistics
from typing import Optional

from sqlalchemy import func

from app.models import Conversation, Lead, Message

logger = logging.getLogger("response_times")

WAITING_ALERT = datetime.timedelta(minutes=15)  # depois disso o "aguardando" fica vermelho
IDLE_ALERT = datetime.timedelta(days=3)  # sem interação nossa há mais que isso: alerta


def mark_inbound(lead: Lead, when: Optional[datetime.datetime] = None) -> None:
    lead.last_inbound_at = when or datetime.datetime.utcnow()


def mark_outbound(lead: Lead, when: Optional[datetime.datetime] = None) -> None:
    when = when or datetime.datetime.utcnow()
    lead.last_outbound_at = when
    if lead.first_response_at is None:
        lead.first_response_at = when


def first_response(lead: Lead) -> Optional[datetime.timedelta]:
    if lead.first_response_at is None:
        return None
    return max(lead.first_response_at - lead.created_at, datetime.timedelta(0))


def waiting_since(lead: Lead) -> Optional[datetime.datetime]:
    """Desde quando o cliente espera resposta nossa (None = não está esperando)."""
    if lead.first_response_at is None:
        return lead.created_at
    if lead.last_inbound_at and (lead.last_outbound_at is None or lead.last_inbound_at > lead.last_outbound_at):
        return lead.last_inbound_at
    return None


def summary(leads: list) -> dict:
    """Números do Dashboard pra uma lista de leads."""
    times = [first_response(l).total_seconds() for l in leads if l.first_response_at is not None]
    now = datetime.datetime.utcnow()
    return {
        "median_first": datetime.timedelta(seconds=statistics.median(times)) if times else None,
        "within_15": round(100 * sum(1 for t in times if t <= 15 * 60) / len(times)) if times else None,
        "answered": len(times),
        "never_answered": sum(1 for l in leads if l.first_response_at is None),
        "idle": sum(1 for l in leads if l.last_outbound_at and now - l.last_outbound_at > IDLE_ALERT),
    }


def backfill(db) -> int:
    """Leads de antes desta função: calcula pelos registros de mensagens."""
    pending = db.query(Lead.id).filter(Lead.first_response_at.is_(None), Lead.last_inbound_at.is_(None)).subquery()
    rows = (
        db.query(
            Conversation.lead_id,
            Message.direction,
            func.min(Message.created_at),
            func.max(Message.created_at),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id.in_(db.query(pending.c.id)), ~Message.body.like("⚠️ Não enviada%"))
        .group_by(Conversation.lead_id, Message.direction)
        .all()
    )
    by_lead: dict = {}
    for lead_id, direction, first, last in rows:
        by_lead.setdefault(lead_id, {})[direction] = (first, last)
    for lead_id, values in by_lead.items():
        update = {}
        if "in" in values:
            update[Lead.last_inbound_at] = values["in"][1]
        if "out" in values:
            update[Lead.first_response_at], update[Lead.last_outbound_at] = values["out"]
        if update:
            update[Lead.updated_at] = Lead.updated_at  # não mexe na "última atividade"
            db.query(Lead).filter(Lead.id == lead_id).update(update, synchronize_session=False)
    db.commit()
    if by_lead:
        logger.info("tempos de atendimento calculados pra %s leads antigos", len(by_lead))
    return len(by_lead)


def human(delta: Optional[datetime.timedelta]) -> str:
    """'agora', '8 min', '2h 10min', '3 dias'."""
    if delta is None:
        return "—"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "menos de 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins:02d}min" if mins else f"{hours}h"
    days = hours // 24
    return "1 dia" if days == 1 else f"{days} dias"


def ago(when: Optional[datetime.datetime]) -> str:
    return human(datetime.datetime.utcnow() - when) if when else "—"
