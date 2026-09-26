"""Ponto único que decide o que fazer quando um lead muda de etapa: registra
a intenção em ConversionEvent e dispara para Meta e/ou Google conforme os
dados de atribuição disponíveis no lead."""

import json

from sqlalchemy.orm import Session

from app.models import ConversionEvent, Lead
from app.services.conversions import google_ads, meta_capi


def dispatch_stage_conversion(db: Session, lead: Lead, event_name: str) -> list[ConversionEvent]:
    attribution = lead.attribution
    events: list[ConversionEvent] = []

    if attribution and (attribution.ctwa_clid or attribution.fbc or attribution.fbp):
        event = ConversionEvent(
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            platform="meta",
            event_name=event_name,
            payload_json=json.dumps({"ctwa_clid": attribution.ctwa_clid, "fbc": attribution.fbc}),
        )
        result = meta_capi.send_event(
            event_name,
            ctwa_clid=attribution.ctwa_clid,
            fbc=attribution.fbc,
            fbp=attribution.fbp,
            phone=lead.phone,
            email=lead.email,
        )
        event.status = "sent" if result["ok"] else "failed"
        event.response_json = json.dumps(result["body"])
        if not result["ok"]:
            event.error = str(result["body"])
        db.add(event)
        events.append(event)

    if attribution and attribution.gclid:
        event = ConversionEvent(
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            platform="google",
            event_name=event_name,
            payload_json=json.dumps({"gclid": attribution.gclid}),
        )
        result = google_ads.send_click_conversion(attribution.gclid)
        event.status = "sent" if result["ok"] else "failed"
        event.response_json = json.dumps(result["body"])
        if not result["ok"]:
            event.error = str(result["body"])
        db.add(event)
        events.append(event)

    if events:
        db.commit()
        for event in events:
            db.refresh(event)

    return events
