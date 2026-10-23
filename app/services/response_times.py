"""Tempo de atendimento: quanto demoramos pra responder o lead pela 1ª vez e há quanto
tempo não falamos com ele. Atualizado a cada mensagem (inbound.py / Inbox)."""

from __future__ import annotations

import datetime
import logging
import statistics
from typing import Optional

from sqlalchemy import and_, func, or_

from app.models import Conversation, Lead, Message

logger = logging.getLogger("response_times")

WAITING_ALERT = datetime.timedelta(minutes=15)  # depois disso o "aguardando" fica vermelho
IDLE_ALERT = datetime.timedelta(days=3)  # sem interação nossa há mais que isso: alerta


FOLLOWUP_BURST = datetime.timedelta(hours=1)  # várias mensagens seguidas contam como uma tentativa


def mark_inbound(lead: Lead, when: Optional[datetime.datetime] = None) -> None:
    lead.last_inbound_at = when or datetime.datetime.utcnow()
    lead.unanswered_outs = 0  # o cliente respondeu


def mark_outbound(lead: Lead, when: Optional[datetime.datetime] = None) -> None:
    when = when or datetime.datetime.utcnow()
    previous = lead.last_outbound_at
    if previous is None or (lead.last_inbound_at and lead.last_inbound_at > previous) or when - previous > FOLLOWUP_BURST:
        lead.unanswered_outs = (lead.unanswered_outs or 0) + 1  # mais uma tentativa nossa sem resposta
    lead.last_outbound_at = when
    if lead.first_response_at is None:
        lead.first_response_at = when


def first_response(lead: Lead) -> Optional[datetime.timedelta]:
    if lead.first_response_at is None:
        return None
    return max(lead.first_response_at - lead.created_at, datetime.timedelta(0))


def is_settled(lead: Lead) -> bool:
    """Atendimento encerrado depois da última mensagem do cliente (não precisa responder)."""
    return bool(lead.settled_at and (lead.last_inbound_at is None or lead.settled_at >= lead.last_inbound_at))


def settle(lead: Lead, user_id: Optional[str] = None) -> None:
    lead.settled_at = datetime.datetime.utcnow()
    lead.settled_by_user_id = user_id


def awaiting_reply_sql():
    """Mesma regra de waiting_since, em SQL: cliente falou por último e ninguém respondeu nem
    encerrou depois. Usada no "Não respondidas", "aguardando resposta" e afins."""
    return and_(
        Lead.last_inbound_at.isnot(None),
        or_(Lead.last_outbound_at.is_(None), Lead.last_inbound_at > Lead.last_outbound_at),
        or_(Lead.settled_at.is_(None), Lead.last_inbound_at > Lead.settled_at),
    )


def waiting_since(lead: Lead) -> Optional[datetime.datetime]:
    """Desde quando o cliente espera resposta nossa (None = não está esperando)."""
    if is_settled(lead):
        return None
    if lead.first_response_at is None:
        return lead.created_at
    if lead.last_inbound_at and (lead.last_outbound_at is None or lead.last_inbound_at > lead.last_outbound_at):
        return lead.last_inbound_at
    return None


DAYS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6}
DEFAULT_HOURS = "seg-sex 07:30-17:30; sab 09:00-16:00"


def parse_hours(text: str) -> dict:
    """"seg-sex 07:30-17:30; sab 09:00-16:00" -> {0: (450, 1050), ..., 5: (540, 960)} em minutos do dia."""
    import re

    out: dict = {}
    for part in (text or DEFAULT_HOURS).lower().split(";"):
        m = re.match(r"\s*(\w+)(?:\s*-\s*(\w+))?\s+(\d{1,2})[:h]?(\d{2})?\s*-\s*(\d{1,2})[:h]?(\d{2})?", part)
        if not m or m.group(1) not in DAYS or (m.group(2) and m.group(2) not in DAYS):
            continue
        first, last = DAYS[m.group(1)], DAYS[m.group(2) or m.group(1)]
        start = int(m.group(3)) * 60 + int(m.group(4) or 0)
        end = int(m.group(5)) * 60 + int(m.group(6) or 0)
        for day in range(first, last + 1):
            out[day] = (start, end)
    return out or parse_hours(DEFAULT_HOURS)


def business_seconds(start: datetime.datetime, end: datetime.datetime, hours: dict) -> float:
    """Segundos entre start e end (UTC) contando só o horário comercial (horário de Brasília)."""
    from app.timeutil import to_local

    a, b = to_local(start), to_local(end)
    if b <= a:
        return 0.0
    total, day = 0.0, a.date()
    while day <= b.date():
        window = hours.get(day.weekday())
        if window:
            open_at = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(minutes=window[0])
            close_at = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(minutes=window[1])
            lo, hi = max(a.replace(tzinfo=None), open_at), min(b.replace(tzinfo=None), close_at)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += datetime.timedelta(days=1)
    return total


def summary(leads: list, hours: Optional[dict] = None) -> dict:
    """Números do Dashboard pra uma lista de leads. hours (parse_hours): também o 1º atendimento
    contando só o horário comercial (lead que chega às 22h e é respondido às 8h = poucos minutos)."""
    answered = [l for l in leads if l.first_response_at is not None]
    times = [first_response(l).total_seconds() for l in answered]
    biz = [business_seconds(l.created_at, l.first_response_at, hours) for l in answered] if hours else []
    now = datetime.datetime.utcnow()
    return {
        "median_first": datetime.timedelta(seconds=statistics.median(times)) if times else None,
        "within_15": round(100 * sum(1 for t in times if t <= 15 * 60) / len(times)) if times else None,
        "median_first_biz": datetime.timedelta(seconds=statistics.median(biz)) if biz else None,
        "within_15_biz": round(100 * sum(1 for t in biz if t <= 15 * 60) / len(biz)) if biz else None,
        "answered": len(times),
        "never_answered": sum(1 for l in leads if l.first_response_at is None and not is_settled(l)),
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
