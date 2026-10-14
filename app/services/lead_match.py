"""Um lead por pessoa: formulário da LP e conversa de WhatsApp caem no MESMO lead,
casados pelo telefone (com ou sem o 9º dígito, que o WhatsApp às vezes omite)."""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Lead, UtmAttribution
from app.services import deals, funnel
from app.services.attribution import _phone_variants, normalize_br_phone
from app.services.distribution import assign_lead
from app.timeutil import to_local


def find_lead_by_phone(db: Session, tenant_id: str, phone: str) -> Optional[Lead]:
    """Negócio atual do cliente com esse telefone: o de número mais alto (recompra); entre
    cadastros duplicados antigos, o mais antigo."""
    if not phone:
        return None
    variants = _phone_variants(phone) | _phone_variants(normalize_br_phone(phone))
    return (
        db.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.phone.in_(variants))
        .order_by(Lead.deal_number.desc(), Lead.created_at)
        .first()
    )


FORM_LABELS = [
    ("marca", "Marca"),
    ("modelo", "Modelo"),
    ("ano", "Ano"),
    ("pecas", "Peças"),
    ("qtd_pecas_encontradas", "Peças encontradas no estoque"),
    ("mensagem", "Mensagem"),
    ("pagina", "Página"),
]


def _format_details(data: dict) -> str:
    when = to_local(datetime.datetime.utcnow()).strftime("%d/%m/%Y %H:%M")
    lines = [f"📝 Formulário enviado em {when}"]
    for key, label in FORM_LABELS:
        value = str(data.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def upsert_form_lead(db: Session, tenant_id: str, data: dict, source: str = "site_form") -> tuple:
    """Cria ou completa o lead a partir de um formulário. Devolve (lead, criado_agora).
    data: name, phone, email, utm_*, gclid, fbclid, fbc, fbp + campos extras do formulário."""
    phone = normalize_br_phone(data.get("phone", ""))
    lead = find_lead_by_phone(db, tenant_id, phone)
    if lead is not None and deals.should_reopen(lead):
        lead = deals.open_new(db, lead, by_customer=True)  # cliente antigo preencheu de novo: negócio novo
    if lead is None and data.get("email"):
        lead = (
            db.query(Lead)
            .filter(Lead.tenant_id == tenant_id, Lead.email == data["email"].strip().lower())
            .order_by(Lead.created_at)
            .first()
        )

    created = lead is None
    if created:
        first_stage = funnel.entry_stage(db, tenant_id, with_chat=False)  # ainda não falou no WhatsApp
        lead = Lead(tenant_id=tenant_id, phone=phone, source=source, stage_id=first_stage.id if first_stage else None)
        db.add(lead)

    # o nome digitado no formulário vale mais que o apelido do WhatsApp
    if data.get("name"):
        lead.name = data["name"].strip()
    if data.get("email") and not lead.email:
        lead.email = data["email"].strip().lower()
    if not lead.phone and phone:
        lead.phone = phone
    lead.archived_at = None
    lead.deleted_at = None
    details = _format_details(data)
    lead.form_details = f"{details}\n\n{lead.form_details}".strip() if lead.form_details else details
    db.commit()
    db.refresh(lead)

    utm_keys = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid", "fbc", "fbp")
    if lead.attribution is None and any(data.get(k) for k in utm_keys):
        db.add(UtmAttribution(lead_id=lead.id, **{k: str(data.get(k) or "")[:250] for k in utm_keys}))
        db.commit()

    if created or not lead.assigned_user_id:
        assign_lead(db, lead)
    return lead, created


def merge_lid_leads(db: Session) -> list:
    """Leads que nasceram com o código LID do WhatsApp no lugar do telefone (a mensagem chegou
    antes de o CRM saber de quem era): agora que o telefone é conhecido (lid_map), junta as
    conversas no lead certo, ou só corrige o telefone se a pessoa não tem outro cadastro.
    Devolve [(nome, "juntou" | "corrigiu", vendedor de antes, vendedor de agora)]."""
    from app.models import Conversation, LidMap, Message

    known = {row.lid: row.phone for row in db.query(LidMap)}
    if not known:
        return []
    done = []
    wrong = db.query(Lead).filter(Lead.phone.in_(list(known)), Lead.deleted_at.is_(None), Lead.is_group.is_(False)).all()
    for lead in wrong:
        phone = known[lead.phone]
        real = next((l for l in [find_lead_by_phone(db, lead.tenant_id, phone)] if l is not None and l.id != lead.id), None)
        before = lead.assigned_user.name if lead.assigned_user else "—"
        if real is None:
            lead.phone = phone
            done.append((lead.name or phone, "corrigiu", before, before))
            continue
        for conv in list(lead.conversations):
            same = next((c for c in real.conversations if c.whatsapp_number_id == conv.whatsapp_number_id), None)
            if same is None:
                real.conversations.append(conv)  # muda de dono pela relação (não deixa "órfã")
                if hasattr(conv, "assigned_user_id"):
                    conv.assigned_user_id = real.assigned_user_id
                continue
            db.query(Message).filter(Message.conversation_id == conv.id).update(
                {Message.conversation_id: same.id}, synchronize_session=False
            )
            if conv.last_message_at and (same.last_message_at is None or conv.last_message_at > same.last_message_at):
                same.last_message_at, same.last_preview = conv.last_message_at, conv.last_preview
            db.flush()
            db.delete(conv)
        for field in ("last_inbound_at", "last_outbound_at", "last_message_at"):
            if hasattr(real, field) and getattr(lead, field, None) and (getattr(real, field) is None or getattr(lead, field) > getattr(real, field)):
                setattr(real, field, getattr(lead, field))
        if lead.first_response_at and (real.first_response_at is None or lead.first_response_at < real.first_response_at):
            real.first_response_at = lead.first_response_at
        if lead.attribution is not None and real.attribution is None:
            lead.attribution.lead_id = real.id
        lead.deleted_at = datetime.datetime.utcnow()  # fica na Lixeira, vazio
        db.flush()
        conv = db.query(Conversation).filter(Conversation.lead_id == real.id).order_by(Conversation.last_message_at.desc()).first()
        if conv is not None:
            db.add(Message(conversation_id=conv.id, direction="note",
                           body="🔗 Mensagens que tinham caído num cadastro separado (código interno do WhatsApp no lugar do telefone) foram juntadas aqui"))
        after = real.assigned_user.name if real.assigned_user else "—"
        done.append((real.name or phone, "juntou", before, after))
    db.commit()
    return done
