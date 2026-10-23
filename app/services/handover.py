"""📱 Conversa que ficou presa num WhatsApp que não manda mais (vendedor removido, número desligado):
passa pro WhatsApp do vendedor que agora é o responsável pelo lead, pra equipe conseguir responder
pelo CRM. O histórico continua todo no CRM; no celular do cliente vira uma conversa nova, com o
número do vendedor novo.

Só troca quando o número atual NÃO manda (removido do CRM ou desconectado) E é de outra pessoa:
número do próprio vendedor que caiu por um tempo não troca (é só reconectar), e conversa que o
admin passou pra outro vendedor sem trocar o número continua no número de antes."""

from __future__ import annotations

import datetime
from typing import Optional

from app.models import Conversation, Lead, Message, WhatsAppNumber


def usable(number: Optional[WhatsAppNumber]) -> bool:
    return (number is not None and number.is_active and bool(number.phone_number)
            and (number.provider != "evolution" or number.connection_state == "open"))


def own_number(db, user_id: Optional[str], tenant_id: str) -> Optional[WhatsAppNumber]:
    """WhatsApp conectado dessa pessoa (ou None)."""
    if not user_id:
        return None
    return next((n for n in db.query(WhatsAppNumber).filter(
        WhatsAppNumber.tenant_id == tenant_id, WhatsAppNumber.owner_user_id == user_id, WhatsAppNumber.is_active.is_(True))
        if usable(n)), None)


def next_number(db, lead: Lead) -> Optional[WhatsAppNumber]:
    """Pra qual WhatsApp a conversa precisa passar antes da próxima mensagem (None = fica como está)."""
    conv = lead.active_conversation
    current = conv.whatsapp_number if conv is not None else None
    if conv is None or usable(current):
        return None
    if current is not None and current.is_active and current.owner_user_id == lead.assigned_user_id:
        return None  # é do próprio vendedor e só caiu: reconectar resolve
    target = own_number(db, lead.assigned_user_id, lead.tenant_id)
    return target if target is not None and target.id != (current.id if current else None) else None


def move_to(db, lead: Lead, number: WhatsAppNumber, why: str) -> Conversation:
    """As próximas mensagens desse lead saem por `number` (conversa nova nesse número ou a que já existia)."""
    previous = lead.active_conversation
    conversation = next((c for c in lead.conversations if c.whatsapp_number_id == number.id), None)
    if conversation is None:
        conversation = Conversation(tenant_id=lead.tenant_id, lead_id=lead.id, whatsapp_number_id=number.id,
                                    assigned_user_id=lead.assigned_user_id,
                                    last_preview=previous.last_preview if previous else "")
        db.add(conversation)
    conversation.assigned_user_id = lead.assigned_user_id
    conversation.last_message_at = datetime.datetime.utcnow()  # vira a conversa ativa
    db.flush()
    old = previous.whatsapp_number.label if previous is not None and previous.whatsapp_number else "o número anterior"
    db.add(Message(conversation_id=conversation.id, direction="note",
                   body=f"📱 A conversa passou do {old} pro {number.label}: {why}. O histórico continua aqui; "
                        "no celular do cliente as próximas mensagens chegam desse número novo."))
    db.flush()
    db.refresh(lead)
    return conversation


def ensure_sendable(db, lead: Lead) -> Optional[Conversation]:
    """Antes de responder: se a conversa está presa num WhatsApp que não manda mais, passa pro do
    responsável. Devolve a conversa por onde a mensagem vai sair."""
    target = next_number(db, lead)
    if target is None:
        return lead.active_conversation
    current = lead.active_conversation.whatsapp_number
    why = "o WhatsApp anterior foi removido do CRM" if current is None or not current.is_active else "o WhatsApp anterior está desconectado"
    return move_to(db, lead, target, why)
