"""📵 Contato inválido: o WhatsApp diz que o número não existe, mesmo tentando com e sem o 9º dígito
(número digitado errado no formulário, ou sem WhatsApp). O lead vai pra Perdido com o motivo
"Contato inválido". WhatsApp do vendedor desconectado NUNCA conta (o problema é nosso, não do número),
nem lead com o código interno do WhatsApp (LID) no lugar do telefone, nem grupo."""

from __future__ import annotations

import re

from app import segments
from app.models import Conversation, Lead, Message


def looks_like_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    return 10 <= len(digits) <= 13  # 14+ dígitos = código interno do WhatsApp (LID), não telefone


def mark(db, lead: Lead, why: str = "o WhatsApp diz que esse número não existe (testamos com e sem o 9º dígito)") -> bool:
    """Leva o lead pra Perdido · Contato inválido. Devolve True se mudou."""
    from app.services import funnel

    stage = lead.stage
    if lead.is_group or stage is None or stage.is_won or stage.is_lost or not looks_like_phone(lead.phone):
        return False
    lost = next((s for s in funnel._stages(db, lead.tenant_id) if s.is_lost), None)
    if lost is None:
        return False
    funnel.set_stage(lead, lost)
    lead.loss_reason = segments.INVALID_REASON
    lead.loss_detail = f"automático: {why}"
    conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
    if conv is not None:
        db.add(Message(conversation_id=conv.id, direction="note",
                       body=f"📵 Perdido · Contato inválido: {why}. Se o cliente mandar mensagem de outro número, ou o número "
                            "for corrigido, é só mudar a etapa."))
    db.add(lead)
    return True


def after_failed_send(db, lead: Lead) -> bool:
    """Chame depois de um envio que falhou: se foi "número não existe", marca o lead."""
    from app.services import messaging

    if lead is not None and messaging.last_error() == messaging.NO_WHATSAPP:
        return mark(db, lead)
    return False
