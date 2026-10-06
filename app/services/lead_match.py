"""Um lead por pessoa: formulário da LP e conversa de WhatsApp caem no MESMO lead,
casados pelo telefone (com ou sem o 9º dígito, que o WhatsApp às vezes omite)."""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Lead, PipelineStage, UtmAttribution
from app.services.attribution import _phone_variants, normalize_br_phone
from app.services.distribution import assign_lead
from app.timeutil import to_local


def find_lead_by_phone(db: Session, tenant_id: str, phone: str) -> Optional[Lead]:
    """Lead do cliente com esse telefone (o mais antigo, se por acaso houver mais de um)."""
    if not phone:
        return None
    variants = _phone_variants(phone) | _phone_variants(normalize_br_phone(phone))
    return (
        db.query(Lead)
        .filter(Lead.tenant_id == tenant_id, Lead.phone.in_(variants))
        .order_by(Lead.created_at)
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
    if lead is None and data.get("email"):
        lead = (
            db.query(Lead)
            .filter(Lead.tenant_id == tenant_id, Lead.email == data["email"].strip().lower())
            .order_by(Lead.created_at)
            .first()
        )

    created = lead is None
    if created:
        first_stage = (
            db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant_id).order_by(PipelineStage.order).first()
        )
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
