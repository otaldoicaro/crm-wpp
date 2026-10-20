"""💬 Comentários da equipe num lead (estilo Google Docs): só no CRM, nunca no WhatsApp.

Admin deixa uma instrução ("oferece o kit completo"), o vendedor responde e alguém marca como
feito. O rastro fica no lead (Inbox e "Status do lead"). Quem precisa ver recebe a conversa como
não lida: o vendedor do lead e quem já escreveu naquele comentário."""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.models import Lead, LeadComment, User
from app.services import inbox_state

MAX_LEN = 2000


def thread(db: Session, lead: Lead) -> list:
    """[{"comment", "replies"}]: em aberto primeiro (mais antigo no topo), depois os feitos (mais recente no topo)."""
    rows = (
        db.query(LeadComment)
        .options(joinedload(LeadComment.author), joinedload(LeadComment.resolved_by))
        .filter(LeadComment.lead_id == lead.id, LeadComment.tenant_id == lead.tenant_id)
        .order_by(LeadComment.created_at)
        .all()
    )
    replies: dict = {}
    for row in rows:
        if row.parent_id:
            replies.setdefault(row.parent_id, []).append(row)
    tops = [r for r in rows if not r.parent_id]
    open_ = [r for r in tops if r.resolved_at is None]
    done = sorted((r for r in tops if r.resolved_at is not None), key=lambda r: r.resolved_at, reverse=True)
    return [{"comment": r, "replies": replies.get(r.id, [])} for r in open_ + done]


def open_count(db: Session, lead: Lead) -> int:
    return db.query(LeadComment.id).filter(
        LeadComment.lead_id == lead.id, LeadComment.parent_id.is_(None), LeadComment.resolved_at.is_(None)).count()


def _notify(db: Session, lead: Lead, root_id: str, author: User) -> None:
    people = {lead.assigned_user_id} if lead.assigned_user_id else set()
    people |= {uid for (uid,) in db.query(LeadComment.author_user_id).filter(
        (LeadComment.id == root_id) | (LeadComment.parent_id == root_id)).distinct() if uid}
    people.discard(author.id)
    for uid in people:
        inbox_state.mark_unread(db, uid, lead.id)


def add(db: Session, lead: Lead, author: User, body: str, parent_id: str = "") -> Optional[LeadComment]:
    """Comentário novo ou resposta (parent_id). Responder um comentário feito reabre ele."""
    text = (body or "").strip()[:MAX_LEN]
    if not text:
        return None
    parent = None
    if parent_id:
        parent = db.query(LeadComment).filter(LeadComment.id == parent_id, LeadComment.lead_id == lead.id,
                                              LeadComment.parent_id.is_(None)).first()
        if parent is None:
            return None
        parent.resolved_at, parent.resolved_by_user_id = None, None
    comment = LeadComment(tenant_id=lead.tenant_id, lead_id=lead.id, parent_id=parent.id if parent else None,
                          author_user_id=author.id, body=text)
    db.add(comment)
    db.flush()
    _notify(db, lead, parent.id if parent else comment.id, author)
    db.commit()
    return comment


def set_done(db: Session, lead: Lead, comment_id: str, user: User, done: bool) -> bool:
    comment = db.query(LeadComment).filter(LeadComment.id == comment_id, LeadComment.lead_id == lead.id,
                                           LeadComment.parent_id.is_(None)).first()
    if comment is None:
        return False
    if done:
        comment.resolved_at, comment.resolved_by_user_id = datetime.datetime.utcnow(), user.id
        if comment.author_user_id and comment.author_user_id != user.id:
            inbox_state.mark_unread(db, comment.author_user_id, lead.id)  # quem pediu vê que foi feito
    else:
        comment.resolved_at, comment.resolved_by_user_id = None, None
    db.commit()
    return True
