"""🗑️ Lixeira com prazo (recurso "lixeira_apaga"): lead excluído há mais de KEEP_DAYS some de vez,
com a conversa, as mensagens e o resto que é só dele. Até lá dá pra restaurar em Pipeline > Lixeira.
Cliente sem o recurso: a Lixeira guarda pra sempre (como sempre foi)."""

from __future__ import annotations

import datetime
import logging

from app.models import (
    Appointment, Conversation, ConversionEvent, InboxRead, Lead, LeadComment, Message, Notification, Tenant,
    UtmAttribution,
)

logger = logging.getLogger("trash")

KEEP_DAYS = 30


def purge_date(lead) -> datetime.datetime | None:
    return lead.deleted_at + datetime.timedelta(days=KEEP_DAYS) if lead.deleted_at else None


def purge(db, now: datetime.datetime | None = None) -> int:
    from app import features

    cutoff = (now or datetime.datetime.utcnow()) - datetime.timedelta(days=KEEP_DAYS)
    gone = 0
    for tenant in db.query(Tenant).all():
        if not features.has(tenant, "lixeira_apaga"):
            continue
        ids = [row.id for row in db.query(Lead.id).filter(
            Lead.tenant_id == tenant.id, Lead.deleted_at.isnot(None), Lead.deleted_at < cutoff)]
        for start in range(0, len(ids), 200):
            chunk = ids[start:start + 200]
            conversations = [row.id for row in db.query(Conversation.id).filter(Conversation.lead_id.in_(chunk))]
            if conversations:
                db.query(Message).filter(Message.conversation_id.in_(conversations)).delete(synchronize_session=False)
            for model in (Conversation, UtmAttribution, ConversionEvent, InboxRead, LeadComment, Notification, Appointment):
                db.query(model).filter(model.lead_id.in_(chunk)).delete(synchronize_session=False)
            db.query(Lead).filter(Lead.id.in_(chunk)).delete(synchronize_session=False)
            db.commit()
        if ids:
            logger.info("lixeira: %s leads excluídos há mais de %s dias apagados de vez em %s", len(ids), KEEP_DAYS, tenant.name)
        gone += len(ids)
    return gone
