"""Pipeline comum de mensagens de WhatsApp, usado pelos dois canais
(Cloud API oficial em webhooks_whatsapp.py e Evolution em webhooks_evolution.py):

mensagem recebida -> acha/cria Lead -> atribuição de origem -> rodízio de
atendente -> Conversation -> Message.
"""

from __future__ import annotations

import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Conversation, Lead, Message, WhatsAppNumber
from app.services import attribution, autostage, deals, followup, funnel, ownership, response_times, suggestions
from app.services.distribution import assign_lead
from app.services.lead_match import find_lead_by_phone


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
        ownership.flush_pending(db, lead, conversation)  # troca de vendedor de antes da conversa vira nota
        db.commit()
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
    secret: str = "",
    when: Optional[datetime.datetime] = None,
) -> Optional[Message]:
    """when: hora original (recuperação de histórico); padrão = agora."""
    if message_exists(db, wa_message_id):
        return None  # webhook reenviado

    lead = find_lead_by_phone(db, number.tenant_id, from_phone)

    if lead is None:
        first_stage = funnel.entry_stage(db, number.tenant_id, with_chat=True)  # já chegou falando: "Novo"
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
        if autostage.refund_if_asked(db, lead, body, outbound=False):
            pass  # comprou e pediu o dinheiro de volta: a venda vira Perdido (não abre negócio novo)
        elif deals.should_reopen(lead, when):
            # cliente que já fechou (ganho/perdido) há mais de 7 dias voltou: negócio novo
            lead = deals.open_new(db, lead, by_customer=True)
        else:
            # fechado há pouco e mandou pedido novo (carro, peça, preço): volta pro atendimento
            lead = autostage.reopen_if_request(db, lead, body, when)
        if not lead.conversations:
            # lead que nasceu do formulário da LP e agora chamou no WhatsApp: o mesmo lead
            if lead.attribution is None and referral:
                attribution.attribution_from_ctwa_referral(db, lead, referral)
            elif lead.attribution is None:
                attribution.attribution_from_click_bridge(db, lead, body)
            if number.owner_user_id and number.owner and number.owner.is_active:
                previous = lead.assigned_user_id
                lead.assigned_user_id = number.owner_user_id  # quem atende é o dono do WhatsApp que recebeu
                ownership.changed(db, lead, previous, f"o cliente chamou no WhatsApp de {number.owner.name} "
                                                      "(a conversa fica com o dono do celular onde ela está)")
        if profile_name and not lead.name:
            lead.name = profile_name
        if lead.archived_at:
            lead.archived_at = None  # cliente voltou a falar: volta pro Pipeline
        if lead.deleted_at:
            lead.deleted_at = None  # idem pra quem estava na Lixeira (fica registrado em deleted_by)
        db.add(lead)

    conversation = _get_or_create_conversation(db, number, lead)
    if when is None or lead.last_inbound_at is None or when > lead.last_inbound_at:
        response_times.mark_inbound(lead, when)
    funnel.on_message(db, lead, outbound=False)  # cliente falou: "Lead sem conversa" -> "Novo"
    followup.on_inbound_text(db, lead, body)  # perguntou o preço: vai pra "Qualificado"
    suggestions.on_message(lead, body, outbound=False)  # 💡 dados do carro, "já comprei", "pago na hora"...
    db.add(lead)
    message = Message(
        conversation_id=conversation.id,
        direction="in",
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
        secret=secret,
    )
    if when is not None:
        message.created_at = when  # mensagem recuperada: fica na hora em que aconteceu
    moment = when or datetime.datetime.utcnow()
    if conversation.last_message_at is None or moment >= conversation.last_message_at:
        conversation.last_message_at = moment
        conversation.last_preview = body[:200]
    db.add(message)
    db.add(conversation)
    db.commit()
    return message


def _unconfirmed_match(db: Session, lead: Lead, body: str, media_type: str,
                       when: Optional[datetime.datetime] = None) -> Optional[Message]:
    """Mensagem enviada pelo CRM que ficou sem confirmação/"não enviada" (servidor lento) e é a mesma
    que o WhatsApp está avisando agora — ou que a recuperação achou (when): mesmo texto ou mesmo
    tipo de mídia, até 15 min antes. Assim ela é confirmada em vez de duplicada."""
    moment = when or datetime.datetime.utcnow()
    since = moment - datetime.timedelta(minutes=15)
    candidates = (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id == lead.id, Message.direction == "out", Message.wa_message_id == "",
                Message.created_at >= since, Message.created_at <= moment + datetime.timedelta(minutes=2))
        .order_by(Message.created_at)
        .all()
    )
    text = (body or "").strip()
    for m in candidates:
        from app.services import messaging

        own = messaging.strip_failed(m.body)
        if (media_type and m.media_type == media_type) or (not media_type and not m.media_type and own.strip() == text):
            return m
    return None


def record_outbound_from_phone(
    db: Session,
    number: WhatsAppNumber,
    to_phone: str,
    wa_message_id: str,
    body: str,
    media_id: str = "",
    media_type: str = "",
    secret: str = "",
    when: Optional[datetime.datetime] = None,
) -> Optional[Message]:
    """Mensagem que o próprio número mandou pelo app do celular (só existe no
    Evolution — na API oficial não dá pra usar o app ao mesmo tempo). Entra no
    histórico do CRM pra conversa ficar completa. Só registra se já existe um
    lead com esse telefone: conversas pessoais do celular não viram lead."""
    if message_exists(db, wa_message_id):
        return None  # já registrada quando foi enviada pelo próprio CRM
    lead = find_lead_by_phone(db, number.tenant_id, to_phone)
    if lead is None or lead.deleted_at:
        return None

    conversation = _get_or_create_conversation(db, number, lead)
    pending = _unconfirmed_match(db, lead, body, media_type, when)
    if when is None or lead.last_outbound_at is None or when > lead.last_outbound_at:
        response_times.mark_outbound(lead, when)  # vendedor respondeu pelo celular
    funnel.on_message(db, lead, outbound=True)  # time falou: vai pra "Em atendimento"
    followup.on_outbound_text(db, lead, body)  # mandou preço pelo celular: vai pra "Negociando"
    suggestions.on_message(lead, body, outbound=True)  # 💡 "temos sim", "não temos", "pedido confirmado"...
    autostage.refund_if_asked(db, lead, body, outbound=True)  # "estorno feito": a venda vira Perdido
    db.add(lead)
    if pending is not None:
        # era a mensagem que o CRM mandou e ficou "⏳ confirmando envio": confirma ela (não duplica)
        pending.wa_message_id, pending.ack, pending.secret = wa_message_id, max(pending.ack, 1), secret or pending.secret
        if pending.body.startswith("⚠️"):
            pending.body = body
        if media_type and not pending.media_id:
            pending.media_id = media_id
        db.commit()
        return pending
    message = Message(
        conversation_id=conversation.id,
        direction="out",
        wa_message_id=wa_message_id,
        body=body,
        media_id=media_id,
        media_type=media_type,
        secret=secret,
        ack=0 if when is not None else 1,  # recuperada: não se sabe se foi lida
    )
    if when is not None:
        message.created_at = when  # mensagem recuperada: fica na hora em que aconteceu
    moment = when or datetime.datetime.utcnow()
    if conversation.last_message_at is None or moment >= conversation.last_message_at:
        conversation.last_message_at = moment
        conversation.last_preview = body[:200]
    db.add(message)
    db.add(conversation)
    db.commit()
    return message
