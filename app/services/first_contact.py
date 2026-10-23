"""🤖 1ª mensagem automática pro lead que preencheu o formulário e não chamou no WhatsApp.

Muitos preenchem a LP e nunca falam com o comercial. Se o cliente ligou (Tenant.auto_hello_enabled),
X minutos depois do formulário, ainda sem conversa, sai uma mensagem do WhatsApp do vendedor do
rodízio confirmando a peça, e o lead vai pra Qualificado (o cliente já disse carro, ano e peça).

Regras:
- uma vez por negócio (Lead.auto_hello_at), só lead em "Lead sem conversa" e sem nenhuma conversa;
- sai do WhatsApp do vendedor do lead; se ele não tem número conectado, vai pro próximo número do
  rodízio (o mesmo do link /go) e o lead passa pra esse vendedor;
- só de HOURS[0] às HOURS[1] (horário de Brasília): formulário de madrugada recebe às 8h;
- formulário com mais de MAX_AGE sem mensagem: desiste (não manda "vou verificar" dias depois);
- quem informou a peça (ou o carro) recebe a confirmação do que pediu e vai pra Qualificado; quem não
  informou recebe a mensagem genérica do ramo ("me passa qual peça, o modelo e o ano") e vai só pra
  Em atendimento (ainda não sabemos o que ele quer);
- número que o WhatsApp diz que não existe: tenta com e sem o 9º dígito antes de desistir.
Os leads antigos sem conversa são chamados uma vez pelo scripts/chamar_sem_conversa.py.
"""

from __future__ import annotations

import datetime
import logging
import re
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, PipelineStage, Tenant, User, WhatsAppNumber
from app.timeutil import to_local

logger = logging.getLogger("first_contact")

HOURS = (8, 21)  # das 8h às 20h59
MAX_AGE = datetime.timedelta(hours=24)

DEFAULT_TEXT = (
    "Fala, {nome}! Tudo bem?\n"
    "Aqui é o {vendedor}, da {empresa}.\n"
    "\n"
    "Vi que você se interessou pela peça {peca}, ano {ano}, né?\n"
    "Vou verificar aqui pra você"
)
PLACEHOLDERS = ("nome", "vendedor", "empresa", "peca", "ano", "marca", "modelo", "carro")


def default_text(tenant: Tenant) -> str:
    """Texto padrão do ramo do cliente (autopeças: DEFAULT_TEXT)."""
    from app import segments

    return segments.segment_for(tenant).get("auto_hello_text") or DEFAULT_TEXT


def generic_text(tenant: Tenant) -> str:
    """Mensagem pra quem não disse a peça/carro: a do cliente (⚙ Configurações) ou a 1ª mensagem do ramo."""
    from app import segments

    if (tenant.auto_hello_generic_text or "").strip():
        return tenant.auto_hello_generic_text
    return (segments.segment_for(tenant).get("first_message") or "").replace("Olá{nome}", "Olá, {nome}")


def has_specific(fields: dict) -> bool:
    """O formulário diz o que a pessoa quer (peça ou carro)?"""
    return bool(part_names(fields.get("peca", "")) or car_names(fields.get("carro", "")))


def target_stage_name(tenant: Tenant) -> str:
    from app import segments

    return segments.segment_for(tenant).get("auto_hello_stage") or "Qualificado"


def _first_name(name: str) -> str:
    first = (name or "").strip().split(" ")[0]
    return first.capitalize() if first.isupper() or first.islower() else first


def form_fields(form_details: str) -> dict:
    """Campos do formulário mais recente (o 1º bloco de Lead.form_details)."""
    block = (form_details or "").split("\n\n")[0]
    labels = {"Marca": "marca", "Modelo": "modelo", "Ano": "ano", "Peças": "peca", "Carros de interesse": "carro"}
    found = {}
    for line in block.splitlines():
        label, _, value = line.partition(": ")
        if label in labels and value.strip():
            found[labels[label]] = value.strip()
    return found


def part_names(raw: str) -> str:
    """"Lanterna Traseira Virtus 2018/22 | SKU 41151 | R$ 299,00" -> "Lanterna Traseira Virtus 2018/22"
    (sem SKU, preço nem código da peça; várias peças separadas por ";" viram "A e B")."""
    names = []
    for item in re.split(r"\s*;\s*|\n", raw or ""):
        name = re.sub(r"\s*R\$\s*[\d.,]+", "", item.split(" | ")[0]).strip(" -|,")
        # código da peça em qualquer lugar ("... 2020/24 26296817", "... 2013/18 - 2265106526 Preto",
        # "... 5U0945096A"): não vai pro cliente. Ano ("2013/18") e motor ("1.4") ficam
        name = re.sub(r"(?:\s+-)?\s+(?=[A-Za-z0-9-]*\d{5})[A-Za-z0-9-]{6,}(?=\s|$)", "", name)
        name = re.sub(r"\s{2,}", " ", name).strip(" -|,")
        if name:
            names.append(name)
    return ", ".join(names[:-1]) + " e " + names[-1] if len(names) > 1 else (names[0] if names else "")


def car_names(raw: str) -> str:
    """"Fiat Argo 2025 (123) | Renault Kwid 2024 (456)" -> "Fiat Argo 2025 e pelo Renault Kwid 2024"
    (sem o código; feito pra frase "pelo {carro}", que fica "pelo Fiat Argo 2025 e pelo Renault Kwid 2024")."""
    names = [re.sub(r"\s*\([^)]*\)\s*$", "", item).strip() for item in (raw or "").split("|")]
    names = [n for n in names if n][:3]
    return ", pelo ".join(names[:-1]) + " e pelo " + names[-1] if len(names) > 1 else (names[0] if names else "")


def compose(template: str, values: dict) -> str:
    """Preenche o texto. Sem o nome do cliente, só some o nome ("Fala! Tudo bem?"); sem outro
    dado (peça, ano...), a linha inteira sai, pra não mandar frase pela metade."""
    text = template or DEFAULT_TEXT
    if not values.get("nome"):
        text = re.sub(r",?\s*\{nome\}", "", text)
    lines = []
    for line in text.split("\n"):
        keys = re.findall(r"\{(\w+)\}", line)
        if any(k in PLACEHOLDERS and not values.get(k) for k in keys):
            continue
        for k in keys:
            if k in PLACEHOLDERS:
                line = line.replace("{" + k + "}", values[k])
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def message_for(tenant: Tenant, lead: Lead, seller: User | None) -> str:
    fields = form_fields(lead.form_details)
    template = (tenant.auto_hello_text or default_text(tenant)) if has_specific(fields) else generic_text(tenant)
    return compose(template, {
        "nome": _first_name(lead.name),
        "vendedor": _first_name(seller.name) if seller else "",
        "empresa": tenant.name,
        "peca": part_names(fields.get("peca", "")),
        "ano": fields.get("ano", ""),
        "marca": fields.get("marca", ""),
        "modelo": fields.get("modelo", ""),
        "carro": car_names(fields.get("carro", "")),
    })


def _connected(number: WhatsAppNumber | None) -> bool:
    return (number is not None and number.is_active and bool(number.phone_number)
            and (number.provider != "evolution" or number.connection_state == "open"))


def is_seller(user: User | None) -> bool:
    """Vendedor que pode receber lead: ativo, na equipe, no rodízio e NÃO admin (admin não atende)."""
    return (user is not None and user.role == "agent" and user.is_active and not user.removed_at
            and user.accepting_leads and not user.read_only)


def seller_numbers(db, tenant_id: str) -> list:
    """WhatsApps conectados de vendedores (nunca de admin), quem recebeu há mais tempo primeiro."""
    numbers = [n for n in db.query(WhatsAppNumber).filter(WhatsAppNumber.tenant_id == tenant_id, WhatsAppNumber.is_active.is_(True))
               if _connected(n) and n.owner_user_id and is_seller(n.owner)]
    oldest = datetime.datetime(1970, 1, 1)
    return sorted(numbers, key=lambda n: n.last_routed_at or oldest)


def assign_form_lead(db, lead: Lead) -> None:
    """Lead novo do formulário já nasce com um vendedor que pode mandar a mensagem: no rodízio, com
    WhatsApp conectado, nunca admin (o mesmo rodízio do link /go). Assim quem aparece como responsável
    no Pipeline é quem manda a mensagem automática, pelo número dele. Ninguém assim: rodízio normal."""
    from app.services.distribution import _open_leads_by_user, assign_lead

    load = _open_leads_by_user(db, lead.tenant_id)
    numbers = [n for n in seller_numbers(db, lead.tenant_id)
               if not (n.owner.max_open_leads and load.get(n.owner_user_id, 0) >= n.owner.max_open_leads)]
    if not numbers:
        assign_lead(db, lead)
        return
    number = numbers[0]
    number.last_routed_at = datetime.datetime.utcnow()
    lead.assigned_user_id = number.owner_user_id
    db.commit()


def number_for(db, lead: Lead) -> WhatsAppNumber | None:
    """WhatsApp do vendedor do lead; lead de admin, sem vendedor, ou com o WhatsApp do vendedor
    desconectado: o próximo vendedor do rodízio (e o lead passa pra ele)."""
    if lead.assigned_user_id and is_seller(lead.assigned_user):
        own = next((n for n in db.query(WhatsAppNumber).filter(
            WhatsAppNumber.tenant_id == lead.tenant_id, WhatsAppNumber.owner_user_id == lead.assigned_user_id,
            WhatsAppNumber.is_active.is_(True)) if _connected(n)), None)
        if own is not None:
            return own
    candidates = seller_numbers(db, lead.tenant_id)
    if not candidates:
        return None
    number = candidates[0]
    number.last_routed_at = datetime.datetime.utcnow()
    previous, before = lead.assigned_user_id, lead.assigned_user
    lead.assigned_user_id = number.owner_user_id
    from app.services import ownership

    why = ("estava sem vendedor" if before is None else f"{before.name} é admin" if before.role == "admin"
           else f"{before.name} está fora do rodízio" if not is_seller(before)
           else f"o WhatsApp de {before.name} estava desconectado")
    ownership.changed(db, lead, previous, f"mensagem automática do formulário: {why}")
    return number


def waiting_query(db, tenant: Tenant):
    """Leads deste cliente em "Lead sem conversa", sem nenhuma conversa e que ainda não receberam a
    mensagem (com peça/carro: confirmação; sem: a mensagem genérica, se o ramo tiver uma)."""
    from app.services import funnel

    stage = funnel.stage_named(db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).all(), funnel.NO_CHAT_NAME)
    if stage is None:
        return None
    return db.query(Lead).filter(
        Lead.tenant_id == tenant.id, Lead.stage_id == stage.id, Lead.auto_hello_at.is_(None),
        Lead.deleted_at.is_(None), Lead.archived_at.is_(None), Lead.is_group.is_(False), Lead.tag != "outro",
        Lead.phone != "", ~Lead.conversations.any(),
        *([] if generic_text(tenant) else [Lead.form_details.like("%Peças: %") | Lead.form_details.like("%Carros de interesse: %")]),
    )


def due_leads(db, tenant: Tenant, now: datetime.datetime) -> list:
    query = waiting_query(db, tenant)
    if query is None:
        return []
    wait = datetime.timedelta(minutes=max(1, tenant.auto_hello_minutes or 3))
    return query.filter(Lead.created_at <= now - wait, Lead.created_at >= now - MAX_AGE).order_by(Lead.created_at).limit(20).all()


def send_one(db, tenant: Tenant, lead: Lead, number: WhatsAppNumber | None = None) -> str:
    """Manda a mensagem deste lead. Devolve "enviada", "confirmando", "falhou" ou "sem número"."""
    from app.routers.dashboard import _record_outbound
    from app.services import funnel, messaging

    number = number or number_for(db, lead)
    if number is None:
        db.commit()  # pode ter passado o lead pro próximo do rodízio
        return "sem número"
    if number.owner_user_id and number.owner_user_id != lead.assigned_user_id:
        # regra: quem manda é sempre o responsável pelo lead (nunca sai do número de outra pessoa)
        from app.services import ownership

        previous = lead.assigned_user_id
        lead.assigned_user_id = number.owner_user_id
        ownership.changed(db, lead, previous, "mensagem automática sai sempre pelo WhatsApp do responsável")
    seller = db.get(User, number.owner_user_id) if number.owner_user_id else lead.assigned_user
    text = message_for(tenant, lead, seller)
    sender = seller or SimpleNamespace(id=None)  # número compartilhado sem dono
    lead.auto_hello_at = datetime.datetime.utcnow()  # antes de mandar: nunca manda duas vezes
    db.commit()
    ok, wa_id, secret, used = messaging.send_text_any(number, lead.phone, text)
    if ok is not False and used != lead.phone:
        lead.phone = used  # o número certo era com (ou sem) o 9º dígito
    conversation = db.query(Conversation).filter(
        Conversation.lead_id == lead.id, Conversation.whatsapp_number_id == number.id).first()
    if conversation is None:  # o aviso do WhatsApp sobre essa mesma mensagem pode já ter criado
        conversation = Conversation(tenant_id=tenant.id, lead_id=lead.id, whatsapp_number_id=number.id,
                                    assigned_user_id=lead.assigned_user_id)
        db.add(conversation)
        db.flush()
    if ok is False:
        _record_outbound(db, conversation, sender, "", f"⚠️ Não enviada ({messaging.last_error()}): {text}")
        no_whatsapp = messaging.last_error() == messaging.NO_WHATSAPP
        db.add(Message(conversation_id=conversation.id, direction="note",
                       body="📵 A mensagem automática não saiu: esse número não tem WhatsApp (tentamos com e sem o 9º dígito). "
                            "Ligue pro cliente ou confira o número." if no_whatsapp else
                       "🤖 A mensagem automática do formulário não saiu. Confira o número do cliente e clique em 🔄 pra tentar de novo."))
        db.commit()
        return "falhou"
    if ok:
        from app.services import response_times

        response_times.mark_outbound(lead)
    _record_outbound(db, conversation, sender, wa_id, text, secret=secret, pending=ok is None)
    # disse a peça/carro: já está qualificado; mensagem genérica: só Em atendimento (vem do _record_outbound)
    target = funnel.stage_named(funnel._stages(db, tenant.id), target_stage_name(tenant)) \
        if has_specific(form_fields(lead.form_details)) else None
    if target is not None and (lead.stage is None or lead.stage.order < target.order):
        funnel.set_stage(lead, target)
    who = seller.name if seller else number.label
    db.add(Message(conversation_id=conversation.id, direction="note",
                   body=f"🤖 Mensagem automática do formulário enviada pelo WhatsApp de {who}"
                        + (f"; lead passou pra {target.name}" if target is not None else "") + "."))
    db.commit()
    return "confirmando" if ok is None else "enviada"


def failed_query(db, tenant: Tenant):
    """Leads cuja mensagem automática ficou "⚠️ Não enviada" e que nunca falaram com a gente."""
    return db.query(Lead).join(Conversation, Conversation.lead_id == Lead.id).join(
        Message, Message.conversation_id == Conversation.id).filter(
        Lead.tenant_id == tenant.id, Lead.auto_hello_at.isnot(None), Lead.deleted_at.is_(None), Lead.is_group.is_(False),
        Message.direction == "out", Message.wa_message_id == "", Message.body.like("⚠️ Não enviada%"),
        ~Lead.conversations.any(Conversation.messages.any(Message.direction == "in")),
    ).distinct()


def retry_failed(db, tenant: Tenant, lead: Lead) -> str:
    """Tenta de novo a mensagem automática que falhou (agora com e sem o 9º dígito), pelo mesmo
    WhatsApp, e a mesma mensagem da conversa vira enviada. Devolve "enviada", "falhou" ou "pulado"."""
    from app.services import funnel, messaging, response_times

    message = (db.query(Message).join(Conversation, Conversation.id == Message.conversation_id)
               .filter(Conversation.lead_id == lead.id, Message.direction == "out", Message.wa_message_id == "",
                       Message.body.like("⚠️ Não enviada%")).order_by(Message.created_at.desc()).first())
    if message is None:
        return "pulado"
    conversation = db.get(Conversation, message.conversation_id)
    number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
    if not _connected(number):
        return "pulado"
    seller = db.get(User, number.owner_user_id) if number.owner_user_id else lead.assigned_user
    text = message_for(tenant, lead, seller)  # texto de agora (ex: genérica, código da peça já sem)
    ok, wa_id, secret, used = messaging.send_text_any(number, lead.phone, text)
    if ok is False:
        message.body = f"⚠️ Não enviada ({messaging.last_error()}): {text}"
        db.commit()
        return "falhou"
    fixed_phone = used != lead.phone
    if fixed_phone:
        lead.phone = used
    message.body, message.wa_message_id, message.secret = text, wa_id, secret
    message.ack = messaging.UNCONFIRMED if ok is None else 1
    message.created_at = datetime.datetime.utcnow()
    response_times.mark_outbound(lead)
    funnel.on_message(db, lead, outbound=True)
    target = funnel.stage_named(funnel._stages(db, tenant.id), target_stage_name(tenant)) \
        if has_specific(form_fields(lead.form_details)) else None
    if target is not None and (lead.stage is None or lead.stage.order < target.order):
        funnel.set_stage(lead, target)
    db.add(Message(conversation_id=conversation.id, direction="note",
                   body="🤖 Mensagem automática reenviada" + (f" (o número certo era {used}, com/sem o 9º dígito)" if fixed_phone else "") + "."))
    db.commit()
    return "enviada"


def run() -> dict:
    """Uma passada por todos os clientes com a mensagem ligada. {cliente: enviadas}."""
    db = SessionLocal()
    done: dict = {}
    try:
        now = datetime.datetime.utcnow()
        if not HOURS[0] <= to_local(now).hour < HOURS[1]:
            return done
        for tenant in db.query(Tenant).filter(Tenant.auto_hello_enabled.is_(True)).all():
            for lead in due_leads(db, tenant, now):
                try:
                    result = send_one(db, tenant, lead)
                except Exception:
                    db.rollback()
                    logger.exception("mensagem automática: falhou no lead %s", lead.id)
                    continue
                if result in ("enviada", "confirmando"):
                    done[tenant.name] = done.get(tenant.name, 0) + 1
    finally:
        db.close()
    return done
