"""📅 Agendamentos (recurso "agendamentos" do cliente): visita marcada na loja.

- Agendar (topo da conversa ou ao mover pra etapa Agendamento): cliente, carro, possível forma de
  pagamento, data e hora. Leva o lead pra Agendamento e, se marcado, manda a confirmação pro cliente.
- Lembrete no dia: X horas antes (Tenant.agenda_reminder_hours), pelo WhatsApp da conversa.
- 🔔 10 minutos antes: aviso no canto da tela pro vendedor e pros admins.
- Na hora marcada vira "🟡 Visita a confirmar"; 6h depois sem ninguém marcar se veio, "⏰ Prazo atrasado".

Tudo em UTC no banco; data/hora que a pessoa digita e vê é horário de Brasília (timeutil)."""

from __future__ import annotations

import datetime
import logging
import re
from types import SimpleNamespace
from typing import Optional

from app import features, segments
from app.models import Appointment, Lead, Message, Tenant, User
from app.timeutil import local_to_utc, to_local

logger = logging.getLogger("agenda")

FEATURE = "agendamentos"
CONFIRM_AFTER = datetime.timedelta(hours=6)  # da hora marcada até 5h59 depois: "a confirmar"
ALERT_BEFORE = datetime.timedelta(minutes=10)
WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
PAYMENTS = ["À vista", "Financiamento", "Parcelado", "Troca + diferença", "Ainda não sabe"]
DONE_LABEL = {"veio": "✅ Veio", "nao_veio": "❌ Não veio", "cancelado": "Cancelado"}


def state(appt: Appointment, now: Optional[datetime.datetime] = None) -> str:
    """agendado | a_confirmar | atrasado | veio | nao_veio | cancelado"""
    if appt.status != "agendado":
        return appt.status
    now = now or datetime.datetime.utcnow()
    if now < appt.at:
        return "agendado"
    return "a_confirmar" if now < appt.at + CONFIRM_AFTER else "atrasado"


def active_for(db, lead_id: str) -> Optional[Appointment]:
    return (db.query(Appointment).filter(Appointment.lead_id == lead_id, Appointment.status == "agendado")
            .order_by(Appointment.at.desc()).first())


def active_by_lead(db, tenant_id: str) -> dict:
    """{lead_id: agendamento em aberto} pro Pipeline."""
    rows = db.query(Appointment).filter(Appointment.tenant_id == tenant_id, Appointment.status == "agendado").order_by(Appointment.at)
    return {a.lead_id: a for a in rows}


def visible(db, tenant: Tenant, user: User):
    """Admin vê os de todos; vendedor, só os dele."""
    query = db.query(Appointment).filter(Appointment.tenant_id == tenant.id)
    if user.role != "admin":
        query = query.filter(Appointment.user_id == user.id)
    return query


def pending_count(db, tenant: Tenant, user: User) -> int:
    """Número da aba lá em cima: agendamentos em aberto (próximos, a confirmar e atrasados)."""
    return visible(db, tenant, user).filter(Appointment.status == "agendado").count()


def when_label(appt: Appointment, now: Optional[datetime.datetime] = None) -> str:
    """"hoje 14:30", "amanhã 10:00", "ontem 15:00", "qua 29/10 16:30"."""
    local, today = to_local(appt.at), to_local(now or datetime.datetime.utcnow()).date()
    days = (local.date() - today).days
    day = {0: "hoje", 1: "amanhã", -1: "ontem"}.get(days) or f"{WEEKDAYS[local.weekday()][:3]} {local:%d/%m}"
    return f"{day} {local:%H:%M}"


def day_title(day: datetime.date, today: datetime.date) -> str:
    names = {0: "Hoje", 1: "Amanhã"}
    prefix = names.get((day - today).days)
    label = f"{WEEKDAYS[day.weekday()]}, {day:%d/%m}"
    return f"{prefix} · {label}" if prefix else label[:1].upper() + label[1:]


def parse_when(date_str: str, time_str: str) -> Optional[datetime.datetime]:
    """"2026-10-27" + "14:30" (horário de Brasília) -> UTC."""
    try:
        local = datetime.datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return local_to_utc(local)


def prefill(lead: Lead) -> dict:
    """Carro (do formulário do site) e pagamento (dos campos de venda parcelada) já preenchidos."""
    from app.services import car_deal, first_contact

    raw = first_contact.form_fields(lead.form_details or "").get("carro", "")
    cars = [re.sub(r"\s*\([^)]*\)\s*$", "", item).strip() for item in raw.split("|")]
    payment = {"avista": "À vista", "parcelado": "Parcelado"}.get(lead.pay_mode or "", "")
    if payment == "Parcelado" and car_deal.describe(lead):
        payment = f"Parcelado · {car_deal.describe(lead)}"
    return {"car": " / ".join(c for c in cars if c)[:200], "payment": payment[:120]}


def texts(tenant: Tenant) -> dict:
    seg = segments.segment_for(tenant)
    return {
        "confirm": tenant.agenda_confirm_text or seg.get("agenda_confirm_text", ""),
        "reminder": tenant.agenda_reminder_text or seg.get("agenda_reminder_text", ""),
        "confirm_default": seg.get("agenda_confirm_text", ""),
        "reminder_default": seg.get("agenda_reminder_text", ""),
    }


def compose(template: str, tenant: Tenant, appt: Appointment, lead: Lead) -> str:
    """Preenche {nome} {data} {hora} {carro} {empresa} {vendedor}; linha com dado que falta sai inteira."""
    from app.services.autofollow import first_name

    local = to_local(appt.at)
    seller = appt.user or lead.assigned_user
    values = {
        "nome": first_name(lead.name), "data": f"{WEEKDAYS[local.weekday()].capitalize()}, {local:%d/%m}",
        "hora": f"{local:%H:%M}", "carro": appt.car, "empresa": tenant.name,
        "vendedor": (seller.name.split(" ")[0] if seller and seller.name else ""),
    }
    text = template or ""
    if not values["nome"]:
        text = re.sub(r"(Oi |Olá )?\{nome\},?\s*", lambda m: (m.group(1) or "").strip() + ("! " if m.group(1) else ""), text)
    lines = []
    for line in text.split("\n"):
        keys = re.findall(r"\{(\w+)\}", line)
        if any(k in values and not values[k] for k in keys):
            continue
        for k, v in values.items():
            line = line.replace("{" + k + "}", v)
        lines.append(line)
    return "\n".join(lines).strip()


def send(db, tenant: Tenant, lead: Lead, appt: Appointment, text: str, what: str) -> bool:
    """Manda pelo WhatsApp da conversa do lead (com nota na conversa). False = não saiu."""
    from app.routers.dashboard import _record_outbound
    from app.services import handover, messaging, response_times

    if not text.strip():
        return False
    conversation = handover.ensure_sendable(db, lead)
    if conversation is None:
        return False
    sender = appt.user or lead.assigned_user or SimpleNamespace(id=None)
    ok, wa_id, secret, used = messaging.send_text_any(conversation.whatsapp_number, lead.phone, text)
    if ok is False:
        _record_outbound(db, conversation, sender, "", f"⚠️ Não enviada ({messaging.last_error()}): {text}")
        return False
    if used != lead.phone:
        lead.phone = used
    response_times.mark_outbound(lead)
    _record_outbound(db, conversation, sender, wa_id, text, secret=secret, pending=ok is None)
    db.add(Message(conversation_id=conversation.id, direction="note", body=f"📅 {what} enviado pro cliente (automático)"))
    return True


def note(db, lead: Lead, text: str) -> None:
    conv = lead.active_conversation
    if conv is not None:
        db.add(Message(conversation_id=conv.id, direction="note", body=text))


def run() -> int:
    """A cada minuto: 🔔 aviso 10 min antes e lembrete pro cliente. Devolve quantas ações fez."""
    from app.db import SessionLocal
    from app.services import notify

    db = SessionLocal()
    done = 0
    try:
        now = datetime.datetime.utcnow()
        for tenant in db.query(Tenant).all():
            if not features.has(tenant, FEATURE):
                continue
            upcoming = db.query(Appointment).filter(
                Appointment.tenant_id == tenant.id, Appointment.status == "agendado",
                Appointment.at > now, Appointment.at <= now + datetime.timedelta(hours=max(tenant.agenda_reminder_hours or 0, 1)),
            ).all()
            admins = [u.id for u in tenant.users if u.role == "admin" and u.is_active and not u.read_only and not u.removed_at]
            for appt in upcoming:
                lead = appt.lead
                try:
                    if appt.alerted_at is None and appt.at - now <= ALERT_BEFORE:
                        seller = appt.user or lead.assigned_user
                        local = to_local(appt.at)
                        body = " · ".join(p for p in (f"{lead.name or lead.phone} vem ver {appt.car}" if appt.car else (lead.name or lead.phone),
                                                       appt.payment, f"vendedor: {seller.name}" if seller else "") if p)
                        for uid in {*(admins), *([seller.id] if seller else [])}:
                            notify.push(db, tenant.id, uid, "agenda", f"🔔 Visita em 10 minutos · {local:%H:%M}", body,
                                        f"/inbox/{lead.id}" if lead.conversations else f"/leads/{lead.id}")
                        appt.alerted_at = now
                        done += 1
                    hours = tenant.agenda_reminder_hours or 0
                    if (appt.reminder and hours and appt.reminder_sent_at is None and appt.at - now <= datetime.timedelta(hours=hours)
                            and to_local(appt.at).date() == to_local(now).date()):
                        appt.reminder_sent_at = now
                        if send(db, tenant, lead, appt, compose(texts(tenant)["reminder"], tenant, appt, lead), "Lembrete da visita"):
                            done += 1
                        else:  # não saiu: uma tentativa só, e o card não diz "lembrete enviado"
                            appt.reminder_sent_at, appt.reminder = None, False
                            note(db, lead, "📅 O lembrete da visita não saiu (WhatsApp não enviou).")
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("agendamento %s: falhou", appt.id)
    finally:
        db.close()
    return done
