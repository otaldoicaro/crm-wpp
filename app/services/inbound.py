"""Pipeline comum de mensagens de WhatsApp, usado pelos dois canais
(Cloud API oficial em webhooks_whatsapp.py e Evolution em webhooks_evolution.py):

mensagem recebida -> acha/cria Lead -> atribuição de origem -> rodízio de
atendente -> Conversation -> Message.
"""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Conversation, Lead, Message, PipelineStage, WhatsAppNumber
from app.services import attribution
from app.services.distribution import assign_lead


def _get_or_create_conversation(db: Session, number: WhatsAppNumber, lead: Lead) -> Conversation:
    conversation = (
        db.query(Conversation)
        .filter(Conversation.lead_id == lead.id, Conversation.whatsapp_number_id == number.id)
        .first()
    )
    if not conversation:
        conversation = Conversation(
            tenant_id=number.tenant_id,
            lead_id=lead.id,
            whatsapp_number_id=number.id,
            assigned_user_id=lead.assigned_user_id,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    return conversation


def message_exists(db: Session, wa_message_id: str) -> bool:
    return bool(wa_message_id) and db.query(Message.id).filter(Message.wa_message_id == wa_message_id).first() is not None


def ingest_inbound(
    db: Session,
    number: WhatsAppNumber,
    from_phone: str,
    wa_message_id: str,
    body: str,
    media_id: str = "",
    media_type: str = "",
    profile_name: str = "",
    referral: Optional[dict] = None,
) -> Optional[Message]:
    if message_exists(db, wa_message_id):
        return None  # webhook reenviado

    lead = db.query(Lead).filter(Lead.tenant_id == number.tenant_id, Lead.phone == from_phone).first()

    if lead is None:
        first_stage = (
            db.query(PipelineStage)
            .filter(PipelineStage.tenant_id == number.tenant_id)
            .order_by(PipelineStage.order)
            .first()
        )
        lead = Lead(
            tenant_id=number.tenant_id,
            name=profile_name,
            phone=from_phone,
            source="whatsapp",
            whatsapp_number_id=number.id,
            stage_id=first_stage.id if first_stage else None,
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)

        if referral:
            attribution.attribution_from_ctwa_referral(db, lead, referral)
        else:
            attribution.attribution_from_click_bridge(db, lead, body)

        assign_lead(db, lead, number)
    else:
        if profile_name and not lead.name:
            lead.name = profile_name
        if lead.archived_at:
            lead.archived_at = None  # cliente voltou a falar: volta pro Pipeline
        if lead.deleted_at:
            lead.deleted_at = None  # idem pra quem estava na Lixeira (fica registrado em deleted_by)
        db.add(lead)

    conversation = _get_or_create_conversation(db, number, lead)
    message = Message(
        conversation_id=conversation.id,
        direction="in",
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
    )
    conversation.last_message_at = datetime.datetime.utcnow()
    conversation.last_preview = body[:200]
    db.add(message)
    db.add(conversation)
    db.commit()
    return message


def record_outbound_from_phone(
    db: Session,
    number: WhatsAppNumber,
    to_phone: str,
    wa_message_id: str,
    body: str,
    media_id: str = "",
    media_type: str = "",
) -> Optional[Message]:
    """Mensagem que o próprio número mandou pelo app do celular (só existe no
    Evolution — na API oficial não dá pra usar o app ao mesmo tempo). Entra no
    histórico do CRM pra conversa ficar completa. Só registra se já existe um
    lead com esse telefone: conversas pessoais do celular não viram lead."""
    if message_exists(db, wa_message_id):
        return None  # já registrada quando foi enviada pelo próprio CRM
    lead = db.query(Lead).filter(Lead.tenant_id == number.tenant_id, Lead.phone == to_phone).first()
    if lead is None or lead.deleted_at:
        return None

    conversation = _get_or_create_conversation(db, number, lead)
    message = Message(
        conversation_id=conversation.id,
        direction="out",
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
    )
    conversation.last_message_at = datetime.datetime.utcnow()
    conversation.last_preview = body[:200]
    db.add(message)
    db.add(conversation)
    db.commit()
    return message
