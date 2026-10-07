"""Negócios (recompra). Cada card do Pipeline é um negócio; a mesma pessoa pode ter vários
(1ª compra, 2ª compra...). Todos apontam pro 1º pelo contact_id, e a conversa do WhatsApp
fica sempre no negócio mais recente — o histórico inteiro continua num lugar só.

- Cliente com negócio FECHADO (Ganho/Perdido) há mais de REOPEN_AFTER_DAYS dias que volta a
  mandar mensagem: abre um negócio novo sozinho, em "Novo".
- Antes disso (ex: "obrigado, chegou certinho") não abre nada; o vendedor pode abrir na mão
  ("+ Novo negócio")."""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import Conversation, Lead, Message
from app.services import funnel

REOPEN_AFTER_DAYS = 7


def contact_key(lead: Lead) -> str:
    return lead.contact_id or lead.id


def is_closed(lead: Lead) -> bool:
    return bool(lead.stage and (lead.stage.is_won or lead.stage.is_lost))


def should_reopen(lead: Lead, now: Optional[datetime.datetime] = None) -> bool:
    """Cliente voltou a falar: abre negócio novo? (fechado há mais de 7 dias)"""
    if lead.is_group or lead.deleted_at or not is_closed(lead) or lead.closed_at is None:
        return False
    return (now or datetime.datetime.utcnow()) - lead.closed_at > datetime.timedelta(days=REOPEN_AFTER_DAYS)


def history(db: Session, lead: Lead) -> list:
    """Todos os negócios da pessoa, do 1º ao atual."""
    key = contact_key(lead)
    return (
        db.query(Lead)
        .filter(Lead.tenant_id == lead.tenant_id, or_(Lead.id == key, Lead.contact_id == key))
        .order_by(Lead.deal_number, Lead.created_at)
        .all()
    )


def summary(db: Session, lead: Lead) -> dict:
    """Pra ficha/Inbox: quantos negócios, quantas compras e quanto já comprou."""
    deals = history(db, lead)
    won = [d for d in deals if d.stage and d.stage.is_won]
    return {
        "deals": deals,
        "count": len(deals),
        "purchases": len(won),
        "total": sum(d.deal_value or 0 for d in won),
        "recurring": len(deals) > 1 or len(won) > 1,
    }


def open_new(db: Session, lead: Lead, by_customer: bool, user_id: Optional[str] = None) -> Lead:
    """Abre o próximo negócio da pessoa e passa a conversa pra ele."""
    key = contact_key(lead)
    last_number = (
        db.query(func.max(Lead.deal_number))
        .filter(Lead.tenant_id == lead.tenant_id, or_(Lead.id == key, Lead.contact_id == key))
        .scalar()
        or 1
    )
    stages = funnel._stages(db, lead.tenant_id)
    has_chat = db.query(Conversation.id).filter(Conversation.lead_id == lead.id).first() is not None
    # cliente chamou/preencheu: "Novo" (ou "Lead sem conversa" se nunca falou no WhatsApp).
    # Vendedor abriu na mão: já está atendendo.
    stage = funnel.entry_stage(db, lead.tenant_id, with_chat=has_chat) if by_customer else (
        funnel.in_service_stage(stages) or funnel.entry_stage(db, lead.tenant_id, with_chat=True)
    )
    new = Lead(
        tenant_id=lead.tenant_id,
        name=lead.name,
        phone=lead.phone,
        email=lead.email,
        tag=lead.tag,
        source="recompra",
        whatsapp_number_id=lead.whatsapp_number_id,
        assigned_user_id=lead.assigned_user_id,
        avatar_url=lead.avatar_url,
        avatar_checked_at=lead.avatar_checked_at,
        contact_id=key,
        deal_number=last_number + 1,
        stage_id=stage.id if stage else None,
        reached_order=stage.order if stage else 0,
    )
    if not by_customer:
        new.first_response_at = datetime.datetime.utcnow()  # o vendedor que abriu: ninguém está esperando resposta
    db.add(new)
    db.flush()
    previous = lead.stage.name if lead.stage else "—"
    value = f" R$ {lead.deal_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if lead.deal_value else ""
    for conversation in db.query(Conversation).filter(Conversation.lead_id == lead.id):
        conversation.lead_id = new.id
        conversation.assigned_user_id = new.assigned_user_id
    db.flush()  # a sessão não grava sozinha antes da consulta abaixo
    active = db.query(Conversation).filter(Conversation.lead_id == new.id).order_by(Conversation.last_message_at.desc()).first()
    if active is not None:
        who = "o cliente voltou a falar" if by_customer else "aberto pelo vendedor"
        db.add(Message(
            conversation_id=active.id, direction="note",
            body=f"🔁 {new.deal_number}º negócio ({who}). O anterior ficou em {previous}{value}.",
        ))
    db.commit()
    db.refresh(new)
    return new


def customer_stats(db: Session, tenant_id: str, won_ids: set, date_from=None, date_to=None, seller: str = "", top_owner: str = "") -> dict:
    """Clientes, recompra e LTV. "Compra" = negócio Ganho. Base inteira (desde sempre) pra
    LTV/recompra; o período escolhido vale pra separar vendas de clientes novos x recompras.
    top_owner: se preenchido, a lista de maiores clientes só mostra clientes desse vendedor."""
    import statistics

    query = db.query(Lead).filter(
        Lead.tenant_id == tenant_id, Lead.deleted_at.is_(None), Lead.is_group.is_(False), Lead.tag != "outro",
        Lead.stage_id.in_(won_ids),
    )
    if seller:
        query = query.filter(Lead.assigned_user_id == seller)
    by_contact: dict = {}
    for d in query:
        by_contact.setdefault(contact_key(d), []).append(d)

    def when(d):
        return d.closed_at or d.updated_at or d.created_at

    customers = []
    period = {"new": 0, "new_revenue": 0.0, "repeat": 0, "repeat_revenue": 0.0}
    gaps = []
    for key, purchases in by_contact.items():
        purchases.sort(key=when)
        total = sum(p.deal_value or 0 for p in purchases)
        customers.append({"key": key, "name": purchases[-1].name or purchases[-1].phone, "lead_id": purchases[-1].id,
                          "owner": purchases[-1].assigned_user_id, "purchases": len(purchases), "total": total,
                          "last": when(purchases[-1])})
        if len(purchases) > 1:
            gaps.append((when(purchases[1]) - when(purchases[0])).days)
        for i, p in enumerate(purchases):
            t = when(p)
            if (date_from and t < date_from) or (date_to and t > date_to):
                continue
            kind = "new" if i == 0 else "repeat"
            period[kind] += 1
            period[kind + "_revenue"] += p.deal_value or 0
    n = len(customers)
    repeaters = sum(1 for c in customers if c["purchases"] > 1)
    revenue = sum(c["total"] for c in customers)
    top = sorted((c for c in customers if not top_owner or c["owner"] == top_owner), key=lambda c: c["total"], reverse=True)[:10]
    return {
        "customers": n,
        "repeaters": repeaters,
        "repeat_rate": round(100 * repeaters / n, 1) if n else None,
        "ltv": revenue / n if n else None,
        "avg_purchases": sum(c["purchases"] for c in customers) / n if n else None,
        "days_to_second": statistics.median(gaps) if gaps else None,
        "period": period,
        "ticket_new": period["new_revenue"] / period["new"] if period["new"] else None,
        "ticket_repeat": period["repeat_revenue"] / period["repeat"] if period["repeat"] else None,
        "top": top,
    }
