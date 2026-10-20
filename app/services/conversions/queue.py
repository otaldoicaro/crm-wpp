"""Fila dos eventos do CRM pro Meta: o lead muda de etapa -> registra (ConversionEvent pendente);
a cada minuto um envio em segundo plano manda os pendentes, montando o evento com o lead como
ele está NA HORA (se o vendedor desfez a etapa nesse meio-tempo, não manda).

Só entra na fila:
- cliente com o envio ligado e o pixel conferido (meta_capi.verify_pixel);
- lead que veio de anúncio do Meta (clique pro WhatsApp, fbclid/fbc, ou UTM do Facebook/Instagram);
- lead/cliente (nunca grupo, "Outro" ou lixeira);
- uma vez por lead e por tipo (oportunidade, venda); e só a partir de agora (a revisão em massa
  das etapas não manda nada: evento velho ensinaria o algoritmo errado)."""

from __future__ import annotations

import datetime
import json
import logging
import threading

from sqlalchemy.orm import object_session

from app.db import SessionLocal
from app.models import Conversation, ConversionEvent, Lead, Tenant, WhatsAppNumber
from app.services.conversions import meta_capi

logger = logging.getLogger("conversions")

META_SOURCES = {"facebook", "fb", "ig", "instagram", "meta", "facebook_ads", "an", "msg", "messenger"}
SEND_AFTER = datetime.timedelta(seconds=60)  # tempo pra o vendedor corrigir uma etapa errada
MAX_ATTEMPTS = 3
INTERVAL_SECONDS = 60


def from_meta_ad(lead: Lead) -> bool:
    att = lead.attribution
    if att is None:
        return False
    return bool(att.ctwa_clid or att.ad_id or att.fbclid or att.fbc or (att.utm_source or "").strip().lower() in META_SOURCES)


def route_for(db, lead: Lead, tenant: Tenant) -> str:
    """Pra onde vai o evento deste lead, sempre um destino DESTE cliente:
    - lead de clique pro WhatsApp e o número que recebeu tem conta do WhatsApp (WABA) conferida ->
      conjunto de dados dessa conta, como conversa de WhatsApp ("dataset|waba");
    - senão, o pixel do cliente, se conferido ("pixel|").  "" = não manda."""
    att = lead.attribution
    if att is not None and att.ctwa_clid:
        number_id = lead.whatsapp_number_id
        if not number_id:
            conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.created_at).first()
            number_id = conv.whatsapp_number_id if conv else None
        number = db.get(WhatsAppNumber, number_id) if number_id else None
        if (number is not None and number.tenant_id == tenant.id and number.meta_waba_checked_at
                and number.meta_dataset_id and number.meta_waba_id):
            return f"{number.meta_dataset_id}|{number.meta_waba_id}"
    if tenant.meta_capi_checked_at and tenant.meta_pixel_id:
        return f"{tenant.meta_pixel_id}|"
    return ""


def kind_for_stage(stage, stages) -> str:
    from app.services import funnel

    if stage.is_won:
        return "won"
    negotiation = funnel.stage_named(stages, funnel.NEGOTIATION_NAME, "Negociação", "Negociacao", "Oportunidade")
    return "opportunity" if negotiation is not None and stage.id == negotiation.id else ""


def on_stage(lead: Lead, stage) -> None:
    """Chamado por funnel.set_stage. Não manda nada daqui: só registra na fila."""
    from app.services import autostage, funnel

    if autostage.QUIET:  # revisão em massa: são etapas antigas
        return
    db = object_session(lead)
    if db is None or lead.is_group or lead.deleted_at or lead.tag == "outro":
        return
    tenant = db.get(Tenant, lead.tenant_id)
    if tenant is None or not (tenant.meta_capi_enabled and tenant.meta_capi_token):
        return
    kind = kind_for_stage(stage, funnel._stages(db, lead.tenant_id))
    if not kind or not from_meta_ad(lead):
        return
    destination = route_for(db, lead, tenant)
    if not destination:
        return
    event_name = meta_capi.EVENT_FOR_KIND[kind]
    exists = db.query(ConversionEvent.id).filter(
        ConversionEvent.lead_id == lead.id, ConversionEvent.platform == "meta", ConversionEvent.event_name == event_name,
        ConversionEvent.status.in_(["pending", "sent"]),
    ).first()
    if exists:
        return
    db.add(ConversionEvent(
        tenant_id=lead.tenant_id, lead_id=lead.id, platform="meta", event_name=event_name, status="pending",
        event_id=f"{lead.id}-{kind}", destination=destination,
        payload_json=json.dumps({"kind": kind}),
    ))


def send_pending() -> dict:
    """Manda os pendentes (cada um pro pixel do próprio cliente). Devolve {cliente: enviados}."""
    db = SessionLocal()
    done: dict = {}
    try:
        cutoff = datetime.datetime.utcnow() - SEND_AFTER
        pending = db.query(ConversionEvent).filter(
            ConversionEvent.platform == "meta", ConversionEvent.status == "pending", ConversionEvent.created_at <= cutoff,
        ).order_by(ConversionEvent.created_at).limit(200).all()
        from app.services import funnel

        for ev in pending:
            lead = db.get(Lead, ev.lead_id)
            tenant = db.get(Tenant, ev.tenant_id)
            kind = json.loads(ev.payload_json or "{}").get("kind", "")
            stage_kind = kind_for_stage(lead.stage, funnel._stages(db, lead.tenant_id)) if lead and lead.stage else ""
            reached = lead is not None and (stage_kind == kind or (kind == "opportunity" and stage_kind == "won"))
            # travas: mesmo cliente, mesmo pixel conferido de quando entrou na fila, etapa ainda vale
            if (lead is None or tenant is None or lead.tenant_id != tenant.id or not reached
                    or (kind == "won" and not lead.deal_value) or route_for(db, lead, tenant) != ev.destination):
                ev.status, ev.error = "skipped", "etapa desfeita, sem valor, ou destino do cliente mudou"
                continue
            dest, waba = ev.destination.split("|", 1)
            event = meta_capi.build_event(lead, kind, ev.event_id, int(ev.created_at.timestamp()), waba)
            result = meta_capi.send_to(tenant, dest, [event])
            ev.attempts = (ev.attempts or 0) + 1
            ev.payload_json = json.dumps({"kind": kind, "event": event})
            ev.response_json = json.dumps(result["body"])[:4000]
            if result["ok"]:
                ev.status, ev.sent_at, ev.error = "sent", datetime.datetime.utcnow(), ""
                done[tenant.name] = done.get(tenant.name, 0) + 1
            else:
                ev.error = json.dumps(result["body"])[:1000]
                if ev.attempts >= MAX_ATTEMPTS or result["status_code"] in (400, 403):
                    ev.status = "failed"
        db.commit()
    finally:
        db.close()
    return done


def schedule(delay_seconds: int = 60) -> None:
    def run():
        try:
            sent = send_pending()
            if sent:
                logger.info("conversões enviadas pro Meta: %s", sent)
        except Exception:
            logger.exception("conversões: falhou")
        try:
            from app.services import messaging

            messaging.expire_unconfirmed()  # "⏳ confirmando envio" sem confirmação há 3 min: avisa
        except Exception:
            logger.exception("mensagens sem confirmação: falhou")
        schedule(INTERVAL_SECONDS)

    timer = threading.Timer(delay_seconds, run)
    timer.daemon = True
    timer.start()
