"""Não lidas do Inbox (por pessoa) e etiquetas de contato."""

from __future__ import annotations

import datetime

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models import Conversation, InboxRead, Lead, Message, User

TAGS = [("", "Lead"), ("cliente", "Cliente"), ("outro", "Outro")]
TAG_LABEL = dict(TAGS)


def ensure_baseline(db: Session, user: User) -> None:
    """Na 1ª vez, tudo que já existia conta como lido (não aparece a base inteira como não lida)."""
    if user.inbox_seen_from is None:
        user.inbox_seen_from = datetime.datetime.utcnow()
        db.add(user)
        db.commit()


def _read_row(db: Session, user_id: str, lead_id: str) -> InboxRead:
    row = db.query(InboxRead).filter(InboxRead.user_id == user_id, InboxRead.lead_id == lead_id).first()
    if row is None:
        row = InboxRead(user_id=user_id, lead_id=lead_id)
        db.add(row)
    return row


def mark_read(db: Session, user_id: str, lead_id: str) -> None:
    row = _read_row(db, user_id, lead_id)
    row.last_read_at = datetime.datetime.utcnow()
    row.manual_unread = False
    db.commit()


def mark_unread(db: Session, user_id: str, lead_id: str) -> None:
    row = _read_row(db, user_id, lead_id)
    row.manual_unread = True
    db.commit()


def unread_map(db: Session, user: User, lead_ids: list) -> dict:
    """{lead_id: n} das conversas não lidas por esta pessoa (n=0 + manual = só a bolinha).
    Mensagem do cliente conta como não lida se veio depois de: quando a pessoa leu,
    quando ela começou a usar a função, e da última resposta da equipe (se alguém já
    respondeu, o que veio antes já foi visto). Uma consulta só pra lista inteira."""
    if not lead_ids:
        return {}
    baseline = user.inbox_seen_from or datetime.datetime.utcnow()
    read = InboxRead.__table__.alias("r")
    rows = (
        db.query(Conversation.lead_id, func.count(Message.id))
        .join(Message, Message.conversation_id == Conversation.id)
        .join(Lead, Lead.id == Conversation.lead_id)
        .outerjoin(read, and_(read.c.lead_id == Conversation.lead_id, read.c.user_id == user.id))
        .filter(
            Conversation.lead_id.in_(lead_ids),
            Message.direction == "in",
            Message.created_at > baseline,
            or_(read.c.last_read_at.is_(None), Message.created_at > read.c.last_read_at),
            or_(Lead.last_outbound_at.is_(None), Message.created_at > Lead.last_outbound_at),
        )
        .group_by(Conversation.lead_id)
        .all()
    )
    result = {lead_id: n for lead_id, n in rows if n}
    for (lead_id,) in db.query(InboxRead.lead_id).filter(
        InboxRead.user_id == user.id, InboxRead.lead_id.in_(lead_ids), InboxRead.manual_unread.is_(True)
    ):
        result.setdefault(lead_id, 0)
    return result


def unanswered_filter(query):
    """Conversas em que a última mensagem é do cliente e ninguém da equipe respondeu depois
    (aberta ou não). Usa as colunas do lead: rápido, sem varrer mensagens."""
    return query.filter(
        Lead.last_inbound_at.isnot(None),
        or_(Lead.last_outbound_at.is_(None), Lead.last_inbound_at > Lead.last_outbound_at),
    )
