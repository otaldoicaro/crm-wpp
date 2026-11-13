"""✅ Tarefas dos leads (recurso "tarefas"): criar na conversa/ficha, ver em ✅ Tarefas (atrasadas, hoje,
próximas) e receber o aviso no dia (notificação no canto da tela + número no menu)."""

from __future__ import annotations

import datetime
import logging
import threading

from sqlalchemy import or_

from app.models import Lead, LeadTask
from app.timeutil import to_local

logger = logging.getLogger("tasks")

NOTIFY_FROM_HOUR = 7  # tarefa do dia sem hora: avisa a partir das 7h
INTERVAL_SECONDS = 5 * 60


def today() -> datetime.date:
    return to_local(datetime.datetime.utcnow()).date()


def visible(db, tenant, user):
    """Tarefas que a pessoa vê: admin vê todas; vendedor, as dele."""
    q = db.query(LeadTask).join(Lead, Lead.id == LeadTask.lead_id).filter(
        LeadTask.tenant_id == tenant.id, Lead.deleted_at.is_(None))
    if user.role != "admin":
        q = q.filter(LeadTask.user_id == user.id)
    return q


def buckets(db, tenant, user, only_mine: bool = False) -> dict:
    q = visible(db, tenant, user)
    if only_mine:
        q = q.filter(LeadTask.user_id == user.id)
    day = today()
    pending = q.filter(LeadTask.done_at.is_(None)).order_by(LeadTask.due_date, LeadTask.due_time).all()
    done = q.filter(LeadTask.done_at.isnot(None)).order_by(LeadTask.done_at.desc()).limit(30).all()
    return {
        "late": [t for t in pending if t.due_date < day],
        "today": [t for t in pending if t.due_date == day],
        "next": [t for t in pending if t.due_date > day],
        "done": done,
        "today_date": day,
    }


def due_count(db, tenant, user) -> int:
    """Número do menu: tarefas dela de hoje e atrasadas, ainda não feitas."""
    return (db.query(LeadTask).join(Lead, Lead.id == LeadTask.lead_id)
            .filter(LeadTask.tenant_id == tenant.id, LeadTask.user_id == user.id, LeadTask.done_at.is_(None),
                    LeadTask.due_date <= today(), Lead.deleted_at.is_(None)).count())


def open_for_lead(db, lead) -> list:
    return (db.query(LeadTask).filter(LeadTask.lead_id == lead.id, LeadTask.done_at.is_(None))
            .order_by(LeadTask.due_date, LeadTask.due_time).all())


def when_label(task) -> str:
    day, ref = task.due_date, today()
    if day == ref:
        label = "hoje"
    elif day == ref + datetime.timedelta(days=1):
        label = "amanhã"
    elif day < ref:
        label = f"atrasada ({day.strftime('%d/%m')})"
    else:
        label = day.strftime("%d/%m")
    return label + (f" às {task.due_time}" if task.due_time else "")


def notify_due(db) -> int:
    """Avisa (uma vez) cada tarefa do dia: sem hora, a partir das 7h; com hora, 15 min antes."""
    from app.services import notify

    now_local = to_local(datetime.datetime.utcnow())
    day = now_local.date()
    sent = 0
    rows = (db.query(LeadTask).join(Lead, Lead.id == LeadTask.lead_id)
            .filter(LeadTask.done_at.is_(None), LeadTask.notified_at.is_(None), LeadTask.due_date <= day,
                    Lead.deleted_at.is_(None)).all())
    for task in rows:
        if task.due_date == day:
            if task.due_time:
                try:
                    hh, mm = (int(x) for x in task.due_time.split(":"))
                except ValueError:
                    hh, mm = NOTIFY_FROM_HOUR, 0
                if now_local < now_local.replace(hour=hh, minute=mm, second=0) - datetime.timedelta(minutes=15):
                    continue
            elif now_local.hour < NOTIFY_FROM_HOUR:
                continue
        lead = task.lead
        notify.push(db, task.tenant_id, task.user_id, "tarefa", f"✅ Tarefa {when_label(task)}: {task.title}",
                    body=lead.name or lead.phone or "", url=f"/inbox/{lead.id}" if lead.conversations else f"/leads/{lead.id}")
        task.notified_at = datetime.datetime.utcnow()
        sent += 1
    db.commit()
    return sent


def schedule(delay_seconds: int = 60) -> None:
    def run():
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            notify_due(db)
        except Exception:
            logger.exception("tarefas: aviso falhou")
        finally:
            db.close()
        schedule(INTERVAL_SECONDS)

    timer = threading.Timer(delay_seconds, run)
    timer.daemon = True
    timer.start()
