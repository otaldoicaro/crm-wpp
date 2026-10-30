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
from app.services.inbox_state import NOT_SALES_TAGS


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


def private(lead: Lead) -> bool:
    """🔒 Contato "Time" ou "Outro": cada vendedor tem a conversa DELE com essa pessoa, separada.
    Ex.: o gerente que fala com 2 vendedores — um nunca vê o que o outro conversou."""
    return (lead.tag or "") in NOT_SALES_TAGS and not lead.is_group


def _owner_id(conv: Conversation) -> Optional[str]:
    return conv.whatsapp_number.owner_user_id if conv.whatsapp_number else None


def visible_conversations(lead: Lead, user) -> list:
    """Conversas desse contato que a pessoa pode ver. Admin vê todas; em contato Time/Outro o vendedor
    vê só a do WhatsApp dele (e a de número sem dono, se o contato é dele). O histórico de cada um continua."""
    if getattr(user, "role", "") == "admin" or not private(lead):
        return list(lead.conversations)
    user_id = getattr(user, "id", None)
    return [c for c in lead.conversations
            if _owner_id(c) == user_id or (_owner_id(c) is None and lead.assigned_user_id == user_id)]


def visible_messages(lead: Lead, user) -> list:
    if getattr(user, "role", "") == "admin" or not private(lead):
        return lead.all_messages
    return sorted((m for c in visible_conversations(lead, user) for m in c.messages), key=lambda m: m.created_at)


def ensure_sendable(db, lead: Lead, user=None) -> Optional[Conversation]:
    """Antes de responder: vendedor que tem conversa com esse cliente no WhatsApp DELE responde por
    ela (cliente que fala com 2 vendedores). Senão: se a conversa está presa num WhatsApp que não
    manda mais, passa pro do responsável. Devolve a conversa por onde a mensagem vai sair."""
    if user is not None and getattr(user, "role", "") == "agent":
        own = next((c for c in lead.conversations if c.whatsapp_number and c.whatsapp_number.owner_user_id == user.id
                    and usable(c.whatsapp_number)), None)
        if own is not None:
            return own
        if private(lead):  # 🔒 Time/Outro: nunca responde pela conversa de outro vendedor
            mine = visible_conversations(lead, user)
            return mine[-1] if mine else None
    target = next_number(db, lead)
    if target is None:
        return lead.active_conversation
    current = lead.active_conversation.whatsapp_number
    why = "o WhatsApp anterior foi removido do CRM" if current is None or not current.is_active else "o WhatsApp anterior está desconectado"
    return move_to(db, lead, target, why)


def other_sellers(db, lead: Lead, user) -> list:
    """👥 Outros vendedores (fora quem está olhando) que têm conversa com esse cliente no WhatsApp deles,
    e o responsável, se não for quem está olhando. [{"name", "since", "is_owner"}]"""
    from app.models import User

    if private(lead):
        return []  # 🔒 Time/Outro: cada um tem a sua conversa, não é cliente disputado
    out = {}
    for conv in lead.conversations:
        owner_id = conv.whatsapp_number.owner_user_id if conv.whatsapp_number else None
        if owner_id and owner_id != getattr(user, "id", None) and owner_id not in out:
            owner = db.get(User, owner_id)
            if owner is not None and owner.is_active:
                out[owner_id] = {"name": owner.name, "since": conv.created_at, "is_owner": owner_id == lead.assigned_user_id}
    if len({conv.whatsapp_number.owner_user_id for conv in lead.conversations if conv.whatsapp_number and conv.whatsapp_number.owner_user_id}) < 2:
        return []  # só um vendedor fala com esse cliente: nada a avisar
    return list(out.values())


def shared_lead_ids(db, lead_ids: list) -> set:
    """Leads da lista que falam com 2+ vendedores (ícone 👥 na lista)."""
    from sqlalchemy import func

    if not lead_ids:
        return set()
    rows = (db.query(Conversation.lead_id).join(WhatsAppNumber, WhatsAppNumber.id == Conversation.whatsapp_number_id)
            .join(Lead, Lead.id == Conversation.lead_id)
            .filter(Conversation.lead_id.in_(lead_ids), WhatsAppNumber.owner_user_id.isnot(None), Lead.tag.notin_(NOT_SALES_TAGS))
            .group_by(Conversation.lead_id).having(func.count(func.distinct(WhatsAppNumber.owner_user_id)) > 1).all())
    return {lid for (lid,) in rows}
