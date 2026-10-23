"""🤖 Follow-up automático (recurso "followup_automatico" do cliente, em ⚙ Configurações):

O vendedor mandou preço / forma de pagamento / procedência / entrega e o cliente sumiu:
- 24h sem resposta -> 1ª mensagem ("Oi! Passando para saber se você ainda precisa da peça…");
- mais 24h -> 2ª mensagem ("Fulano, como parece que você não tem interesse mais…");
- mais 6h -> Perdido · "Não responde mais".
O cliente respondeu (ou o vendedor mandou outra mensagem) em qualquer ponto: o fluxo recomeça do zero.

Sai sempre pelo WhatsApp do vendedor do lead, só dentro do horário de atendimento do cliente
(Tenant.business_hours; o prazo que vence fora dele espera a abertura), e só pra conversa cujo
"último recado nosso" foi depois de ligar o recurso (Tenant.autofu_since): nada pra trás."""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from types import SimpleNamespace

from sqlalchemy import or_

from app import features, segments
from app.db import SessionLocal
from app.models import Conversation, Lead, Message, PipelineStage, Tenant

logger = logging.getLogger("autofollow")

FEATURE = "followup_automatico"
FIRST_AFTER = datetime.timedelta(hours=24)
SECOND_AFTER = datetime.timedelta(hours=24)
CLOSE_AFTER = datetime.timedelta(hours=6)
TOLERANCE = datetime.timedelta(minutes=2)  # a nossa própria mensagem automática não conta como "vendedor falou de novo"

DEFAULT_TEXT1 = ("Oi! Passando para saber se você ainda precisa da peça que consultou com a gente.\n\n"
                 "🙂 Se quiser fechar ou tiver alguma dúvida, me chama aqui que dou continuidade no atendimento.")
DEFAULT_TEXT2 = ("{nome}, como parece que você não tem interesse mais, vou encerrar o atendimento aqui, ok?\n"
                 "Mas se precisar de qualquer coisa é só chamar novamente! 🫡")
# o que conta como "mandamos preço/condição e ele sumiu"
TOPIC_WORDS = ("pix", "cartao", "credito", "debito", "parcel", "a vista", "boleto", "link de pagamento", "entrega",
               "frete", "retirada", "garantia", "nota fiscal", "original", "procedencia", "paralelo", "usad",
               "valor", "desconto", "preco", "fica por", "sai por")


def _plain(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().split())


def about_deal(text: str) -> bool:
    from app.services.followup import find_price

    plain = _plain(text)
    return bool(find_price(text or "")) or any(w in plain for w in TOPIC_WORDS)


def first_name(name: str) -> str:
    """"RENAN fernandes" -> "Renan" (sempre a 1ª maiúscula e o resto minúsculo); nome estranho -> ""."""
    first = (name or "").strip().split(" ")[0]
    return first.capitalize() if first.isalpha() and len(first) > 1 else ""


def text_for(tenant: Tenant, lead: Lead, step: int) -> str:
    if step == 1:
        return (tenant.autofu_text1 or DEFAULT_TEXT1).strip()
    text = (tenant.autofu_text2 or DEFAULT_TEXT2).strip()
    name = first_name(lead.name)
    if name:
        return text.replace("{nome}", name)
    # sem nome: "Opa, tudo bem? Como parece que você…"
    text = re.sub(r"^\{nome\},?\s*(\w)", lambda m: "Opa, tudo bem? " + m.group(1).upper(), text)
    return re.sub(r",?\s*\{nome\}", "", text)


def in_hours(tenant: Tenant, now: datetime.datetime) -> bool:
    from app.services.response_times import parse_hours
    from app.timeutil import to_local

    local = to_local(now)
    window = parse_hours(tenant.business_hours).get(local.weekday())
    minute = local.hour * 60 + local.minute
    return bool(window) and window[0] <= minute < window[1]


def _last_outbound_texts(db, lead: Lead) -> list:
    """Nossas mensagens desde a última do cliente (o assunto que ficou sem resposta)."""
    q = (db.query(Message.body, Message.transcript).join(Conversation, Conversation.id == Message.conversation_id)
         .filter(Conversation.lead_id == lead.id, Message.direction == "out", ~Message.body.like("⚠️%")))
    if lead.last_inbound_at:
        q = q.filter(Message.created_at > lead.last_inbound_at)
    return [t or b for b, t in q.order_by(Message.created_at.desc()).limit(10)]


def _send(db, tenant: Tenant, lead: Lead, step: int) -> bool:
    from app.routers.dashboard import _record_outbound
    from app.services import handover, invalid_contact, messaging, response_times

    conversation = handover.ensure_sendable(db, lead)
    if conversation is None:
        return False
    number = conversation.whatsapp_number
    text = text_for(tenant, lead, step)
    sender = lead.assigned_user or SimpleNamespace(id=None)
    ok, wa_id, secret, used = messaging.send_text_any(number, lead.phone, text)
    if ok is False:
        _record_outbound(db, conversation, sender, "", f"⚠️ Não enviada ({messaging.last_error()}): {text}")
        db.add(Message(conversation_id=conversation.id, direction="note",
                       body=f"🤖 O follow-up automático {step}/2 não saiu ({messaging.last_error()}). O fluxo parou nesta conversa."))
        invalid_contact.after_failed_send(db, lead)
        return False
    if used != lead.phone:
        lead.phone = used
    response_times.mark_outbound(lead)
    _record_outbound(db, conversation, sender, wa_id, text, secret=secret, pending=ok is None)
    db.add(Message(conversation_id=conversation.id, direction="note",
                   body=f"🤖 Follow-up automático {step}/2 (o cliente estava sem responder)"
                        + (". Se ele não responder em 6h, o lead vai pra Perdido · Não responde mais." if step == 2 else ".")))
    return True


def run() -> int:
    """Uma passada (a cada minuto). Devolve quantas ações fez."""
    from app.services import funnel
    from app.services.inbox_state import NOT_SALES_TAGS

    db = SessionLocal()
    done = 0
    try:
        now = datetime.datetime.utcnow()
        for tenant in db.query(Tenant).all():
            if not features.has(tenant, FEATURE):
                continue
            if tenant.autofu_since is None:  # acabou de ligar: só daqui pra frente
                tenant.autofu_since = now
                db.commit()
                continue
            open_ids = [s.id for s in db.query(PipelineStage).filter(
                PipelineStage.tenant_id == tenant.id, PipelineStage.is_won.is_(False), PipelineStage.is_lost.is_(False))]
            leads = db.query(Lead).filter(
                Lead.tenant_id == tenant.id, Lead.stage_id.in_(open_ids), Lead.is_group.is_(False),
                Lead.deleted_at.is_(None), Lead.tag.notin_(NOT_SALES_TAGS), Lead.last_outbound_at.isnot(None),
                Lead.last_outbound_at >= tenant.autofu_since, Lead.autofu_step >= 0,
                or_(Lead.last_inbound_at.is_(None), Lead.last_outbound_at > Lead.last_inbound_at),
                Lead.last_outbound_at <= now - min(FIRST_AFTER, CLOSE_AFTER),
            ).limit(300).all()
            hours_open = in_hours(tenant, now)
            for lead in leads:
                step, at = lead.autofu_step or 0, lead.autofu_at
                if step and at and lead.last_outbound_at > at + TOLERANCE:
                    step, lead.autofu_step, lead.autofu_at = 0, 0, None  # o vendedor falou de novo: recomeça
                try:
                    if step == 0 and now - lead.last_outbound_at >= FIRST_AFTER and hours_open:
                        if not about_deal(" ".join(_last_outbound_texts(db, lead))):
                            continue
                        if _send(db, tenant, lead, 1):
                            lead.autofu_step, lead.autofu_at = 1, datetime.datetime.utcnow()
                        else:
                            lead.autofu_step = -1
                        done += 1
                    elif step == 1 and at and now - at >= SECOND_AFTER and hours_open:
                        if _send(db, tenant, lead, 2):
                            lead.autofu_step, lead.autofu_at = 2, datetime.datetime.utcnow()
                        else:
                            lead.autofu_step = -1
                        done += 1
                    elif step == 2 and at and now - at >= CLOSE_AFTER:
                        lost = next((s for s in funnel._stages(db, tenant.id) if s.is_lost), None)
                        if lost is not None:
                            funnel.set_stage(lead, lost)
                            lead.loss_reason = segments.NO_REPLY_REASON
                            lead.loss_detail = "automático: não respondeu os 2 follow-ups"
                            conv = lead.active_conversation
                            if conv is not None:
                                db.add(Message(conversation_id=conv.id, direction="note",
                                               body="🤖 Perdido · Não responde mais: o cliente não respondeu os 2 follow-ups automáticos. "
                                                    "Se ele voltar a falar, o lead volta sozinho pro atendimento."))
                        lead.autofu_step = 3
                        done += 1
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("follow-up automático: falhou no lead %s", lead.id)
    finally:
        db.close()
    return done


def on_inbound(lead: Lead) -> None:
    """O cliente respondeu: o fluxo recomeça do zero na próxima vez que ele sumir."""
    if lead.autofu_step:
        lead.autofu_step, lead.autofu_at = 0, None
