"""🔔 Notificações passageiras (canto da tela, em qualquer página do CRM): o navegador pergunta a
cada ~20 s (/notificacoes) e mostra as novas; cada uma aparece uma vez."""

from __future__ import annotations

import datetime
from typing import Optional

from app.models import Notification


def push(db, tenant_id: str, user_id: Optional[str], kind: str, title: str, body: str = "",
         url: str = "", lead_id: Optional[str] = None) -> None:
    """Cria (não faz commit). Sem pessoa, não faz nada. Não repete a mesma (pessoa, tipo, lead) ainda não vista."""
    if not user_id:
        return
    exists = db.query(Notification.id).filter(
        Notification.user_id == user_id, Notification.kind == kind, Notification.lead_id == lead_id,
        Notification.seen_at.is_(None)).first() if lead_id else None
    if exists:
        return
    db.add(Notification(tenant_id=tenant_id, user_id=user_id, kind=kind, lead_id=lead_id,
                        title=title[:120], body=(body or "")[:300], url=url[:300]))


def pop(db, user_id: str, limit: int = 4) -> list:
    """As novas desta pessoa (marca como vistas)."""
    rows = (db.query(Notification).filter(Notification.user_id == user_id, Notification.seen_at.is_(None))
            .order_by(Notification.created_at).limit(limit).all())
    now = datetime.datetime.utcnow()
    for row in rows:
        row.seen_at = now
    db.commit()
    cutoff = now - datetime.timedelta(days=30)  # faxina
    db.query(Notification).filter(Notification.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return [{"kind": r.kind, "title": r.title, "body": r.body, "url": r.url} for r in rows]
