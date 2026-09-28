import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Conversation, Lead, Message, PipelineStage, Tenant, User, WhatsAppNumber
from app.services.conversions.dispatcher import dispatch_stage_conversion
from app.services.platform import PLATFORM_LABEL, resolve_platform
from app.services.whatsapp_client import send_text_message

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

STAGE_COLOR_PALETTE = ["#4285F4", "#f2a71b", "#8b5cf6", "#22c55e", "#e21b3c", "#06b6d4", "#ec4899"]


def _stage_colors(stages: list[PipelineStage]) -> dict[str, str]:
    return {stage.id: STAGE_COLOR_PALETTE[i % len(STAGE_COLOR_PALETTE)] for i, stage in enumerate(stages)}


@router.get("/", response_class=HTMLResponse)
def pipeline_view(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
    leads = db.query(Lead).filter(Lead.tenant_id == tenant.id).order_by(Lead.updated_at.desc()).all()

    leads_by_stage: dict[str, list[Lead]] = {stage.id: [] for stage in stages}
    for lead in leads:
        if lead.stage_id and lead.stage_id in leads_by_stage:
            leads_by_stage[lead.stage_id].append(lead)

    lead_platforms = {lead.id: resolve_platform(lead.attribution) for lead in leads}

    return templates.TemplateResponse(
        request,
        "pipeline.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "pipeline",
            "stages": stages,
            "leads_by_stage": leads_by_stage,
            "stage_colors": _stage_colors(stages),
            "lead_platforms": lead_platforms,
            "platform_labels": PLATFORM_LABEL,
        },
    )


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(
    lead_id: str,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead:
        return RedirectResponse(url="/")

    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()

    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "pipeline",
            "lead": lead,
            "stages": stages,
            "platform": resolve_platform(lead.attribution),
            "platform_label": PLATFORM_LABEL[resolve_platform(lead.attribution)],
        },
    )


@router.post("/leads/{lead_id}/update")
def update_lead(
    lead_id: str,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    stage_id: str = Form(...),
    deal_value: str = Form(""),
    loss_reason: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    stage = db.query(PipelineStage).filter(PipelineStage.id == stage_id, PipelineStage.tenant_id == tenant.id).first()
    if not lead:
        return RedirectResponse(url="/", status_code=302)

    stage_changed = stage and lead.stage_id != stage.id
    lead.name = name
    lead.phone = phone
    lead.email = email
    lead.loss_reason = loss_reason
    try:
        lead.deal_value = float(deal_value) if deal_value else None
    except ValueError:
        lead.deal_value = None
    if stage:
        lead.stage_id = stage.id
    db.add(lead)
    db.commit()

    if stage_changed and stage.conversion_event_name:
        dispatch_stage_conversion(db, lead, stage.conversion_event_name)

    return RedirectResponse(url=f"/leads/{lead_id}", status_code=302)


@router.post("/leads/{lead_id}/stage")
def change_stage(
    lead_id: str,
    request: Request,
    stage_id: str = Form(...),
    ajax: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    stage = db.query(PipelineStage).filter(PipelineStage.id == stage_id, PipelineStage.tenant_id == tenant.id).first()
    if lead and stage:
        lead.stage_id = stage.id
        db.add(lead)
        db.commit()
        if stage.conversion_event_name:
            dispatch_stage_conversion(db, lead, stage.conversion_event_name)
    if ajax:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=302)


@router.get("/inbox", response_class=HTMLResponse)
def inbox_view(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    return _render_inbox(request, db, tenant, user, None)


@router.get("/inbox/{lead_id}", response_class=HTMLResponse)
def inbox_thread(
    lead_id: str,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    return _render_inbox(request, db, tenant, user, lead_id)


def _render_inbox(request: Request, db: Session, tenant: Tenant, user: User, selected_lead_id: Optional[str]):
    conversations = (
        db.query(Conversation)
        .filter(Conversation.tenant_id == tenant.id)
        .order_by(Conversation.last_message_at.desc())
        .all()
    )

    selected_lead = None
    messages = []
    if selected_lead_id:
        selected_lead = db.query(Lead).filter(Lead.id == selected_lead_id, Lead.tenant_id == tenant.id).first()
        if selected_lead and selected_lead.conversations:
            messages = selected_lead.conversations[0].messages

    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "inbox",
            "conversations": conversations,
            "selected_lead": selected_lead,
            "messages": messages,
            "platform": resolve_platform(selected_lead.attribution) if selected_lead else None,
            "platform_label": PLATFORM_LABEL[resolve_platform(selected_lead.attribution)] if selected_lead else None,
        },
    )


@router.post("/leads/{lead_id}/reply")
def reply_lead(
    lead_id: str,
    body: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead or not lead.conversations:
        return RedirectResponse(url=f"/inbox/{lead_id}", status_code=302)

    conversation = lead.conversations[0]
    number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
    result = send_text_message(number, lead.phone, body)

    message = Message(
        conversation_id=conversation.id,
        direction="out",
        sender_user_id=user.id,
        body=body,
        wa_message_id=result.get("body", {}).get("messages", [{}])[0].get("id", "") if result["ok"] else "",
    )
    conversation.last_message_at = datetime.datetime.utcnow()
    db.add(message)
    db.add(conversation)
    db.commit()

    return RedirectResponse(url=f"/inbox/{lead_id}", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_view(
    request: Request,
    card: str = "all",
    stage_filter: str = "",
    platform_filter: str = "",
    q: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
    leads = db.query(Lead).filter(Lead.tenant_id == tenant.id).all()

    now = datetime.datetime.utcnow()
    last_24h = now - datetime.timedelta(hours=24)

    won_stage_ids = {s.id for s in stages if s.is_won}
    lost_stage_ids = {s.id for s in stages if s.is_lost}
    won_leads = [lead for lead in leads if lead.stage_id in won_stage_ids]

    def is_no_contact_24h(lead: Lead) -> bool:
        return (
            lead.stage_id not in won_stage_ids
            and lead.stage_id not in lost_stage_ids
            and not lead.conversations
            and lead.created_at < last_24h
        )

    # ---- KPIs fixos (sempre sobre a base inteira do tenant) ----
    total_leads = len(leads)
    new_24h = sum(1 for lead in leads if lead.created_at >= last_24h)
    open_no_contact_24h = sum(1 for lead in leads if is_no_contact_24h(lead))
    revenue = sum(lead.deal_value or 0 for lead in won_leads)

    creative_counts: dict[str, int] = {}
    for lead in leads:
        creative = (lead.attribution.utm_content if lead.attribution else "") or (
            lead.attribution.ad_headline if lead.attribution else ""
        )
        if creative:
            creative_counts[creative] = creative_counts.get(creative, 0) + 1
    best_creative = max(creative_counts.items(), key=lambda kv: kv[1])[0] if creative_counts else None

    stage_counts = []
    max_stage_count = max([sum(1 for lead in leads if lead.stage_id == s.id) for s in stages] or [1]) or 1
    for stage in stages:
        count = sum(1 for lead in leads if lead.stage_id == stage.id)
        stage_counts.append(
            {
                "name": stage.name,
                "count": count,
                "pct": round(count / max_stage_count * 100) if max_stage_count else 0,
                "color": _stage_colors(stages)[stage.id],
            }
        )

    platform_counts: dict[str, dict] = {}
    campaign_counts: dict[str, dict] = {}
    for lead in leads:
        platform = resolve_platform(lead.attribution)
        platform_counts.setdefault(platform, {"label": PLATFORM_LABEL[platform], "count": 0})
        platform_counts[platform]["count"] += 1

        campaign = lead.attribution.utm_campaign if lead.attribution and lead.attribution.utm_campaign else None
        if campaign:
            campaign_counts.setdefault(campaign, {"platform": PLATFORM_LABEL[platform], "count": 0, "won": 0})
            campaign_counts[campaign]["count"] += 1
            if lead.stage_id in won_stage_ids:
                campaign_counts[campaign]["won"] += 1

    top_campaigns = sorted(campaign_counts.items(), key=lambda kv: kv[1]["count"], reverse=True)[:8]

    # ---- lista filtrável de leads (cards clicaveis + pills + busca) ----
    table_leads = leads
    if card == "new_24h":
        table_leads = [lead for lead in table_leads if lead.created_at >= last_24h]
    elif card == "no_contact":
        table_leads = [lead for lead in table_leads if is_no_contact_24h(lead)]
    elif card == "won":
        table_leads = [lead for lead in table_leads if lead.stage_id in won_stage_ids]

    if stage_filter:
        table_leads = [lead for lead in table_leads if lead.stage_id == stage_filter]
    if platform_filter:
        table_leads = [lead for lead in table_leads if resolve_platform(lead.attribution) == platform_filter]
    if q:
        needle = q.lower()
        table_leads = [
            lead
            for lead in table_leads
            if needle in (lead.name or "").lower()
            or needle in (lead.phone or "").lower()
            or needle in (lead.email or "").lower()
        ]

    table_leads = sorted(table_leads, key=lambda lead: lead.created_at, reverse=True)[:150]
    stage_by_id = {s.id: s for s in stages}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "dashboard",
            "total_leads": total_leads,
            "new_24h": new_24h,
            "open_no_contact_24h": open_no_contact_24h,
            "won_count": len(won_leads),
            "revenue": revenue,
            "best_creative": best_creative,
            "stage_counts": stage_counts,
            "platform_counts": sorted(platform_counts.items(), key=lambda kv: kv[1]["count"], reverse=True),
            "top_campaigns": top_campaigns,
            "stages": stages,
            "stage_by_id": stage_by_id,
            "table_leads": table_leads,
            "lead_platforms": {lead.id: resolve_platform(lead.attribution) for lead in table_leads},
            "platform_labels": PLATFORM_LABEL,
            "card": card,
            "stage_filter": stage_filter,
            "platform_filter": platform_filter,
            "q": q,
        },
    )
