"""🌙 Mensagem fora do horário (recurso "mensagem_fora_horario" do cliente, em ⚙ Configurações):

O cliente mandou mensagem com a loja fechada (Tenant.business_hours) -> sai uma resposta pelo mesmo
WhatsApp onde ele escreveu (o do vendedor que atende), pedindo o que ele quer e avisando quando voltamos
("amanhã cedo", "segunda cedinho"). Se ele já disse a peça, o carro e o ano, sai a outra mensagem do ramo
("acredito que a gente tenha essa peça… amanhã já confiro pra você"; seminovos não tem).

Regras:
- uma vez por período fechado (Lead.after_hours_at): mandou 5 mensagens de noite, recebe 1 resposta;
- só pra mensagem recente (RECENT): ligar o recurso não responde quem escreveu horas antes;
- não vai pra grupo, nem pra contato Outro/Time, nem se o vendedor falou com o cliente há pouco
  (SELLER_ACTIVE: ele está atendendo mesmo fora do horário);
- não conta como resposta do vendedor: o cliente continua "aguardando resposta" e a etapa não muda.
"""

from __future__ import annotations

import datetime
import logging
import re

from sqlalchemy import or_

from app import features, segments
from app.db import SessionLocal
from app.models import Conversation, Lead, Message, Tenant
from app.timeutil import local_to_utc, to_local

logger = logging.getLogger("after_hours")

FEATURE = "mensagem_fora_horario"
RECENT = datetime.timedelta(minutes=15)
SELLER_ACTIVE = datetime.timedelta(minutes=30)

DEFAULT_TEXT = (
    "Olá, {nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. 😊\n\n"
    "No momento estamos fora do horário de atendimento ({horario}).\n\n"
    "Já me manda aqui a peça que você precisa, o modelo e o ano do carro, que {retorno}, assim que eu voltar, "
    "já verifico pra você.\n\n"
    "Só aguardar que te respondo! 🙏"
)
DEFAULT_DETAIL_TEXT = (
    "Opa, meu amigo(a)! Tudo bem? 😄\n\n"
    "Acredito que a gente tenha essa peça lá sim! Só que agora tô fora da loja e sem acesso ao meu sistema pra "
    "consultar o estoque direitinho.\n\n"
    "{Dia}, a partir das 8h30, já vou estar na loja e consigo conferir pra vc, tá bom?\n\n"
    "Se tiver alguma dúvida ou quiser me passar mais alguma informação sobre a peça, pode mandar por aqui mesmo!\n\n"
    "Assim que eu chegar {dia}, confiro tudo e te dou um retorno. 🤝"
)
DAY_NAMES = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
FULL_DAY_NAMES = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def default_text(tenant: Tenant) -> str:
    return segments.segment_for(tenant).get("after_hours_text") or DEFAULT_TEXT


def detail_default(tenant: Tenant) -> str:
    """Mensagem pra quem já disse peça, carro e ano ("" = o ramo não tem: vai sempre a outra)."""
    return segments.segment_for(tenant).get("after_hours_detail_text", DEFAULT_DETAIL_TEXT)


def says_what_they_want(texts: list) -> bool:
    """A peça, o carro e o ano estão nas mensagens? ("farol do gol 2015", "retrovisor onix 18/19")"""
    from app.services.products import _PART_RE
    from app.services.suggestions import _MODEL, _YEAR, _norm

    plain = _norm(" ".join(t for t in texts if t))
    return bool(_PART_RE.search(plain) and _MODEL.search(plain)
                and (_YEAR.search(plain) or re.search(r"\b\d{2}/\d{2}\b", plain)))


def _hours(tenant: Tenant) -> dict:
    from app.services.response_times import parse_hours

    return parse_hours(tenant.business_hours)


def _clock(minutes: int) -> str:
    """450 -> "07h30", 540 -> "09h"."""
    return f"{minutes // 60:02d}h" + (f"{minutes % 60:02d}" if minutes % 60 else "")


def hours_label(tenant: Tenant) -> str:
    """"seg-sex 07:30-17:30; sab 09:00-16:00" -> "seg a sex das 07h30 às 17h30, sáb das 09h às 16h"."""
    hours, parts, day = _hours(tenant), [], 0
    while day < 7:
        window = hours.get(day)
        if not window:
            day += 1
            continue
        last = day
        while last + 1 < 7 and hours.get(last + 1) == window:
            last += 1
        days = DAY_NAMES[day] if last == day else f"{DAY_NAMES[day]} a {DAY_NAMES[last]}"
        parts.append(f"{days} das {_clock(window[0])} às {_clock(window[1])}")
        day = last + 1
    return ", ".join(parts)


def is_open(tenant: Tenant, now: datetime.datetime) -> bool:
    local = to_local(now)
    window = _hours(tenant).get(local.weekday())
    minute = local.hour * 60 + local.minute
    return bool(window) and window[0] <= minute < window[1]


def closed_since(tenant: Tenant, now: datetime.datetime) -> datetime.datetime:
    """Quando a loja fechou pela última vez (UTC). Sem nenhum horário na semana: 7 dias atrás."""
    local, hours = to_local(now), _hours(tenant)
    for back in range(8):
        day = local.date() - datetime.timedelta(days=back)
        window = hours.get(day.weekday())
        if window:
            end = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(minutes=window[1])
            if end <= local:
                return local_to_utc(end)
    return now - datetime.timedelta(days=7)


def return_day(tenant: Tenant, now: datetime.datetime) -> str:
    """O dia em que a loja abre de novo: "hoje", "amanhã" ou "segunda" (sábado à noite)."""
    phrase = return_phrase(tenant, now)
    return phrase.split(" ")[0] if phrase != "assim que possível" else "logo"


def return_phrase(tenant: Tenant, now: datetime.datetime) -> str:
    """Quando a loja abre de novo: "hoje cedo", "amanhã cedo" ou "segunda cedinho"."""
    local, hours = to_local(now), _hours(tenant)
    for ahead in range(8):
        day = local.date() + datetime.timedelta(days=ahead)
        window = hours.get(day.weekday())
        if window and datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(minutes=window[0]) > local:
            if ahead == 0:
                return "hoje cedo"
            if ahead == 1:
                return "amanhã cedo"
            return f"{FULL_DAY_NAMES[day.weekday()]} cedinho"
    return "assim que possível"


def text_for(tenant: Tenant, lead: Lead, seller_name: str, now: datetime.datetime, detailed: bool = False) -> str:
    from app.services.autofollow import first_name

    detail = (tenant.after_hours_detail_text or detail_default(tenant)).strip() if detailed else ""
    text = detail or (tenant.after_hours_text or default_text(tenant)).strip()
    name, seller = first_name(lead.name), first_name(seller_name)
    if not name:
        text = re.sub(r",?\s*\{nome\}", "", text)  # "Olá! Tudo bem?"
    if not seller:
        text = re.sub(r"\s*Aqui é \{vendedor\}[^.!?\n]*[.!?]?", "", text)  # sai a frase toda ("Aqui é …, da Loja.")
        text = text.replace("{vendedor}", "")
    for key, value in (("nome", name), ("vendedor", seller), ("empresa", tenant.name),
                       ("horario", hours_label(tenant)), ("retorno", return_phrase(tenant, now)),
                       ("dia", return_day(tenant, now)), ("Dia", return_day(tenant, now).capitalize())):
        text = text.replace("{" + key + "}", value)
    return text


def _last_inbound_conversation(db, lead: Lead):
    """A conversa (WhatsApp) onde o cliente escreveu por último: a resposta sai pelo mesmo número."""
    return (db.query(Conversation).join(Message, Message.conversation_id == Conversation.id)
            .filter(Conversation.lead_id == lead.id, Message.direction == "in")
            .order_by(Message.created_at.desc()).first())


def _send(db, tenant: Tenant, lead: Lead, now: datetime.datetime) -> bool:
    from app.services import handover, messaging

    conversation = _last_inbound_conversation(db, lead) or lead.active_conversation
    number = conversation.whatsapp_number if conversation is not None else None
    if not handover.usable(number):
        return False  # WhatsApp desconectado: não tenta outro número de madrugada
    seller = number.owner or lead.assigned_user
    texts = [t or b for b, t in db.query(Message.body, Message.transcript).join(Conversation, Conversation.id == Message.conversation_id)
             .filter(Conversation.lead_id == lead.id, Message.direction == "in", Message.created_at >= closed_since(tenant, now))]
    text = text_for(tenant, lead, seller.name if seller else "", now, detailed=says_what_they_want(texts))
    # o envio não é resposta do vendedor: se o eco do celular marcar como resposta, volta como estava
    before = (lead.last_outbound_at, lead.first_response_at, lead.unanswered_outs)
    ok, wa_id, secret, _used = messaging.send_text_any(number, lead.phone, text)
    if ok is False:
        db.add(Message(conversation_id=conversation.id, direction="note",
                       body=f"🌙 A mensagem fora do horário não saiu ({messaging.last_error()})."))
        return False
    message = db.query(Message).filter(Message.wa_message_id == wa_id).first() if wa_id else None
    message = message or Message(conversation_id=conversation.id, direction="out", wa_message_id=wa_id)
    message.body, message.secret = text, secret or message.secret
    message.ack = max(message.ack or 0, 1) if wa_id else messaging.UNCONFIRMED
    conversation.last_message_at = datetime.datetime.utcnow()
    conversation.last_preview = text[:200]
    db.add(message)
    db.add(Message(conversation_id=conversation.id, direction="note",
                   body="🌙 Mensagem automática fora do horário enviada. O cliente continua aguardando a sua resposta."))
    db.commit()
    db.refresh(lead)
    if (lead.last_outbound_at, lead.first_response_at, lead.unanswered_outs) != before:
        lead.last_outbound_at, lead.first_response_at, lead.unanswered_outs = before
    return True


def run() -> int:
    """Uma passada (a cada minuto). Devolve quantas mensagens mandou."""
    from app.services.inbox_state import NOT_SALES_TAGS

    db = SessionLocal()
    sent = 0
    try:
        now = datetime.datetime.utcnow()
        for tenant in db.query(Tenant).all():
            if not features.has(tenant, FEATURE) or is_open(tenant, now):
                continue
            closed = closed_since(tenant, now)
            leads = db.query(Lead).filter(
                Lead.tenant_id == tenant.id, Lead.is_group.is_(False), Lead.deleted_at.is_(None),
                Lead.tag.notin_(NOT_SALES_TAGS), Lead.last_inbound_at >= max(closed, now - RECENT),
                or_(Lead.after_hours_at.is_(None), Lead.after_hours_at < closed),
                or_(Lead.last_outbound_at.is_(None),
                    (Lead.last_outbound_at < Lead.last_inbound_at) & (Lead.last_outbound_at < now - SELLER_ACTIVE)),
                or_(Lead.settled_at.is_(None), Lead.settled_at < Lead.last_inbound_at),
            ).limit(100).all()
            for lead in leads:
                try:
                    lead.after_hours_at = now  # antes de mandar: nunca manda duas vezes
                    db.commit()
                    if _send(db, tenant, lead, now):
                        sent += 1
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("mensagem fora do horário: falhou no lead %s", lead.id)
    finally:
        db.close()
    return sent
