"""🏷️ Etiquetas do WhatsApp Business viram eventos no CRM.

O vendedor põe (ou tira) uma etiqueta numa conversa no celular / WhatsApp Web -> o Evolution avisa
(eventos LABELS_ASSOCIATION e LABELS_EDIT) -> aparece na conversa do CRM o registro
"🏷️ <vendedor> pôs a etiqueta “Pago” no WhatsApp".

Etiquetas de venda: o cliente escolhe quais são em ⚙ Configurações → Prazos → 🏷️ Etiquetas do WhatsApp
(Tenant.wa_sale_labels, nomes separados por vírgula). Pôr uma delas fecha a venda (etapa Ganho) com o
último preço enviado; sem preço, fecha com valor em branco pro vendedor preencher. No ✋ funil manual
vira 💡 sugestão pro vendedor confirmar. Tirar a etiqueta só registra (não desfaz a venda).

Só registra em quem já é lead (conversa pessoal do celular não vira lead). O nome da etiqueta vem do
próprio evento (LABELS_EDIT) ou da lista do número no Evolution (/label/findLabels), guardada em memória."""

from __future__ import annotations

import logging
import threading
import time
import unicodedata

from app.models import Conversation, Lead, Message, WhatsAppNumber

logger = logging.getLogger("wa_labels")

_names: dict = {}  # instância -> {id da etiqueta: nome}
_fetched: dict = {}  # instância -> quando buscou a lista no Evolution
_lock = threading.Lock()
REFETCH_SECONDS = 600
SAMPLES_LEFT = {"n": 10}  # registra no log o formato dos primeiros eventos (conferir com o Evolution real)


def plain(text: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                            if unicodedata.category(c) != "Mn").split())


def sale_labels(tenant) -> set:
    return {plain(x) for x in (getattr(tenant, "wa_sale_labels", "") or "").split(",") if plain(x)}


def remember(instance: str, label: dict) -> None:
    """LABELS_EDIT: etiqueta criada/renomeada/apagada no celular."""
    label_id = str(label.get("id") or label.get("labelId") or "")
    if not instance or not label_id:
        return
    with _lock:
        names = _names.setdefault(instance, {})
        if label.get("deleted"):
            names.pop(label_id, None)
        elif label.get("name"):
            names[label_id] = str(label["name"])


def labels_of(instance: str, refresh: bool = False) -> dict:
    """{id: nome} das etiquetas do número (busca no Evolution no máximo a cada 10 min)."""
    from app.services import evolution_client

    with _lock:
        fresh = time.time() - _fetched.get(instance, 0) < REFETCH_SECONDS
    if refresh or not fresh:
        try:
            rows = evolution_client.find_labels(instance)
        except evolution_client.EvolutionError as exc:
            logger.warning("wa_labels: não leu as etiquetas de %s: %s", instance, exc)
            rows = None
        with _lock:
            _fetched[instance] = time.time()
            if rows is not None:
                _names[instance] = {r["id"]: r["name"] for r in rows}
    with _lock:
        return dict(_names.get(instance, {}))


def tenant_label_names(db, tenant_id: str) -> list:
    """Nomes das etiquetas dos WhatsApps do cliente (pra tela de configuração escolher as de venda)."""
    seen = {}
    for number in db.query(WhatsAppNumber).filter(WhatsAppNumber.tenant_id == tenant_id, WhatsAppNumber.provider == "evolution",
                                                  WhatsAppNumber.is_active.is_(True), WhatsAppNumber.connection_state == "open"):
        for name in labels_of(number.evolution_instance).values():
            seen.setdefault(plain(name), name)
    return sorted(seen.values(), key=plain)


def _sample(payload) -> None:
    if SAMPLES_LEFT["n"] > 0:
        SAMPLES_LEFT["n"] -= 1
        logger.warning("WA_LABEL_SAMPLE %s", str(payload)[:1500])


def on_edit(number: WhatsAppNumber, data) -> None:
    _sample(data)
    for label in data if isinstance(data, list) else [data]:
        if isinstance(label, dict):
            remember(number.evolution_instance, label)


def on_association(db, number: WhatsAppNumber, data) -> None:
    """LABELS_ASSOCIATION: {"type": "add"|"remove", "chatId": "...", "labelId": "..."} (ou dentro de "association")."""
    from app.routers.webhooks_evolution import _phone_from_key
    from app.services import inbox_state
    from app.services.lead_match import find_lead_by_phone

    _sample(data)
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict):
            continue
        assoc = item.get("association") if isinstance(item.get("association"), dict) else item
        chat_id = str(assoc.get("chatId") or assoc.get("chat_id") or item.get("chatId") or "")
        label_id = str(assoc.get("labelId") or assoc.get("label_id") or item.get("labelId") or "")
        added = str(item.get("type") or assoc.get("type") or "add").lower() != "remove"
        if not chat_id or not label_id or chat_id.endswith(("@g.us", "@broadcast", "@newsletter")):
            continue
        phone = _phone_from_key({"remoteJid": chat_id}, {}, db, number)
        lead = find_lead_by_phone(db, number.tenant_id, phone) if phone else None
        if lead is None or lead.is_group or lead.deleted_at or not inbox_state.mirrored(lead):
            continue
        names = labels_of(number.evolution_instance)
        name = names.get(label_id) or labels_of(number.evolution_instance, refresh=True).get(label_id) or f"#{label_id}"
        apply(db, lead, number, name, added)


def _conversation(db, lead: Lead, number: WhatsAppNumber):
    return (db.query(Conversation).filter(Conversation.lead_id == lead.id, Conversation.whatsapp_number_id == number.id).first()
            or lead.active_conversation)


def apply(db, lead: Lead, number: WhatsAppNumber, name: str, added: bool) -> str:
    """Registra a etiqueta na conversa e, se for de venda, fecha a venda. Devolve o que fez."""
    from app import features
    from app.services import funnel

    conversation = _conversation(db, lead, number)
    if conversation is None:
        return ""
    who = number.owner.name.split(" ")[0] if number.owner is not None and number.owner.name else number.label
    verb = "pôs a etiqueta" if added else "tirou a etiqueta"
    text = f"🏷️ {who} {verb} “{name}” no WhatsApp"
    did = "nota"
    if added and plain(name) in sale_labels(lead.tenant):
        stage = lead.stage
        stages = funnel._stages(db, lead.tenant_id)
        won = next((s for s in stages if s.is_won), None)
        if stage is not None and stage.is_won:
            text += " (a venda já estava marcada)"
        elif won is not None and features.has(lead.tenant, "funil_manual"):
            lead.suggest_kind, lead.suggest_loss_reason = "ganho", ""
            lead.suggest_text = f"etiqueta “{name}” no WhatsApp"[:255]
            text += " — 💡 parece venda: confirme no topo da conversa"
            did = "sugestao"
        elif won is not None:
            value = lead.deal_value or lead.quoted_value or 0
            funnel.set_stage(lead, won)
            if value:
                lead.deal_value = value
                text += f" → 💰 venda fechada ({_brl(value)}, último preço enviado). Se não foi isso, é só mudar a etapa."
            else:
                text += " → 💰 venda fechada. Preencha o valor da venda no topo da conversa."
            if won.conversion_event_name:
                from app.services.conversions.dispatcher import dispatch_stage_conversion

                dispatch_stage_conversion(db, lead, won.conversion_event_name)
            did = "venda"
    db.add(Message(conversation_id=conversation.id, direction="note", body=text[:1000]))
    db.add(lead)
    db.commit()
    return did


def _brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
