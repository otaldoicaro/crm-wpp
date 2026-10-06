"""Arquivamento automático do Pipeline, 1x por dia.

Arquivar NÃO apaga nada: o lead só sai do quadro do Pipeline (que ficaria
pesado e poluído com milhares de cartões). Continua no banco, conta no
Dashboard, aparece em /arquivados (com busca e botão Restaurar) e volta
sozinho pro Pipeline se o cliente mandar mensagem de novo.

Regras (dias configuráveis em app/config.py):
- etapa Ganho/Perdido e sem mudança há ARCHIVE_DONE_DAYS dias;
- etapa em aberto, sem mudança e sem mensagem há ARCHIVE_IDLE_DAYS dias.
"""

from __future__ import annotations

import datetime
import logging
import threading

from sqlalchemy import func

from app.config import ARCHIVE_DONE_DAYS, ARCHIVE_IDLE_DAYS
from app.db import SessionLocal
from app.models import Conversation, Lead, PipelineStage

logger = logging.getLogger("archiver")


def archive_old_leads(db) -> int:
    now = datetime.datetime.utcnow()
    done_cutoff = now - datetime.timedelta(days=ARCHIVE_DONE_DAYS)
    idle_cutoff = now - datetime.timedelta(days=ARCHIVE_IDLE_DAYS)
    terminal = {s.id for s in db.query(PipelineStage).filter(PipelineStage.is_won | PipelineStage.is_lost)}
    last_msg = dict(
        db.query(Conversation.lead_id, func.max(Conversation.last_message_at)).group_by(Conversation.lead_id).all()
    )

    archived = 0
    for lead in db.query(Lead).filter(
        Lead.archived_at.is_(None), Lead.deleted_at.is_(None), Lead.is_group.is_(False), Lead.updated_at < done_cutoff
    ):
        last_activity = max(lead.updated_at, last_msg.get(lead.id) or lead.updated_at)
        if lead.stage_id in terminal:
            should = last_activity < done_cutoff
        else:
            should = last_activity < idle_cutoff
        if should:
            # muda só a coluna, sem mexer no updated_at (que guarda a última atividade real)
            db.query(Lead).filter(Lead.id == lead.id).update(
                {Lead.archived_at: now, Lead.updated_at: lead.updated_at}, synchronize_session=False
            )
            archived += 1
    db.commit()
    return archived


def schedule(delay_seconds: int = 120) -> None:
    def run():
        db = SessionLocal()
        try:
            n = archive_old_leads(db)
            if n:
                logger.info("arquivamento automático: %s leads saíram do Pipeline", n)
        except Exception:
            logger.exception("arquivamento automático falhou")
        finally:
            db.close()
        schedule(delay_seconds=24 * 60 * 60)

    timer = threading.Timer(delay_seconds, run)
    timer.daemon = True
    timer.start()
