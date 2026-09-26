from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Lead, PipelineStage, Tenant, User, WhatsAppNumber
from app.services.conversions.dispatcher import dispatch_stage_conversion
from app.services.whatsapp_client import send_text_message

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
    unstaged: list[Lead] = []
    for lead in leads:
        if lead.stage_id and lead.stage_id in leads_by_stage:
            leads_by_stage[lead.stage_id].append(lead)
        else:
            unstaged.append(lead)

    return templates.TemplateResponse(
        request,
        "pipeline.html",
        {
            "tenant": tenant,
            "user": user,
            "stages": stages,
            "leads_by_stage": leads_by_stage,
            "unstaged": unstaged,
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
    conversation = lead.conversations[0] if lead.conversations else None
    messages = conversation.messages if conversation else []

    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "tenant": tenant,
            "user": user,
            "lead": lead,
            "stages": stages,
            "conversation": conversation,
            "messages": messages,
        },
    )


@router.post("/leads/{lead_id}/stage")
def change_stage(
    lead_id: str,
    stage_id: str = Form(...),
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
    return RedirectResponse(url="/", status_code=302)


@router.post("/leads/{lead_id}/reply")
def reply_lead(
    lead_id: str,
    body: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    from app.models import Message

    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead or not lead.conversations:
        return RedirectResponse(url=f"/leads/{lead_id}", status_code=302)

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
    db.add(message)
    db.commit()

    return RedirectResponse(url=f"/leads/{lead_id}", status_code=302)
