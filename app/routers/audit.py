"""🔎 Auditoria (só admin): quando cada lead entrou e cada contato que ele recebeu.

- /auditoria: leads que entraram no período (hoje, 7 ou 30 dias) ou a busca por nome/telefone, com a hora
  de entrada, o 1º contato (e quanto demorou), quantas mensagens recebeu da loja e se respondeu;
- /auditoria?lead=<id>: a linha do tempo do lead — entrada (origem e formulário), trocas de vendedor e
  cada mensagem (nossa, do cliente, automática), com hora, WhatsApp e quem mandou. Inclui as conversas
  de todos os números (admin vê tudo, também dos contatos Time/Outro)."""

import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Conversation, Lead, Message, Tenant, User
from app.services import ownership
from app.services.platform import PLATFORM_LABEL, resolve_platform
from app.templating import templates
from app.timeutil import local_to_utc, to_local

router = APIRouter()

PERIODS = [("hoje", "Hoje"), ("7d", "7 dias"), ("30d", "30 dias")]
LIST_LIMIT = 200


def _since(period: str) -> datetime.datetime:
    today = to_local(datetime.datetime.utcnow()).date()
    days = {"hoje": 0, "7d": 6, "30d": 29}.get(period, 0)
    return local_to_utc(datetime.datetime.combine(today - datetime.timedelta(days=days), datetime.time.min))


def _summaries(db: Session, lead_ids: list) -> dict:
    """{lead_id: {"first_out", "outs", "ins", "first_in"}} das mensagens de verdade (sem notas)."""
    if not lead_ids:
        return {}
    rows = (db.query(Conversation.lead_id, Message.direction, func.count(Message.id), func.min(Message.created_at))
            .join(Message, Message.conversation_id == Conversation.id)
            .filter(Conversation.lead_id.in_(lead_ids), Message.direction.in_(("in", "out")))
            .group_by(Conversation.lead_id, Message.direction).all())
    out = {}
    for lead_id, direction, count, first in rows:
        item = out.setdefault(lead_id, {"outs": 0, "ins": 0, "first_out": None, "first_in": None})
        item["outs" if direction == "out" else "ins"] = count
        item["first_out" if direction == "out" else "first_in"] = first
    return out


def _timeline(lead: Lead) -> list:
    """[(quando, tipo, título, texto)] em ordem de hora. tipo: entrada | out | in | auto | note | dono."""
    events = [(lead.created_at, "entrada", "Entrou no CRM", "")]
    for conv in lead.conversations:
        number = conv.whatsapp_number.label if conv.whatsapp_number else "WhatsApp removido"
        for msg in conv.messages:
            body = msg.body or (f"[{msg.media_type}]" if msg.media_type else "")
            if msg.direction == "in":
                events.append((msg.created_at, "in", f"Cliente escreveu · {number}", body))
            elif msg.direction == "out":
                who = msg.sender.name if msg.sender else (msg.sender_name or "🤖 automática / celular")
                kind = "falhou" if body.startswith("⚠️ Não enviada") else "out"
                events.append((msg.created_at, kind, f"Loja mandou · {number} · {who}", body))
            elif body.startswith(("🤖", "📅", "↪", "👤", "📵")):
                events.append((msg.created_at, "auto", "Registro do CRM", body))
    events.sort(key=lambda e: e[0] or datetime.datetime.min)
    return events


@router.get("/auditoria", response_class=HTMLResponse)
def audit_page(
    request: Request,
    periodo: str = "hoje",
    q: str = "",
    lead: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só admin vê a Auditoria")
    periodo = periodo if periodo in dict(PERIODS) else "hoje"
    q = q.strip()[:80]
    query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.is_group.is_(False))
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        query = query.filter(or_(Lead.name.ilike(f"%{q}%"), *([Lead.phone.like(f"%{digits}%")] if len(digits) >= 4 else [])))
    else:
        query = query.filter(Lead.created_at >= _since(periodo))
    leads = (query.options(selectinload(Lead.attribution), selectinload(Lead.assigned_user), selectinload(Lead.stage))
             .order_by(Lead.created_at.desc()).limit(LIST_LIMIT).all())
    selected = db.query(Lead).filter(Lead.id == lead, Lead.tenant_id == tenant.id).first() if lead else None
    summaries = _summaries(db, [l.id for l in leads])
    return templates.TemplateResponse(request, "auditoria.html", {
        "tenant": tenant, "user": user, "active_nav": "auditoria", "periodo": periodo, "periods": PERIODS,
        "q": q, "leads": leads, "summaries": summaries, "limit": LIST_LIMIT,
        "platform_of": lambda l: PLATFORM_LABEL[resolve_platform(l.attribution)],
        "selected": selected, "timeline": _timeline(selected) if selected else [],
        "owner_history": ownership.history(selected) if selected else [],
        "selected_platform": PLATFORM_LABEL[resolve_platform(selected.attribution)] if selected else "",
    })
