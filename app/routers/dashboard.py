import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import CampaignSpend, Conversation, Lead, Message, PipelineStage, Tenant, User, WhatsAppNumber
from app.services.conversions.dispatcher import dispatch_stage_conversion
from app.services.platform import PLATFORM_LABEL, resolve_platform
from app.services import media_store, messaging
from app.services.messaging import media_kind_for_mime
from app.templating import templates

router = APIRouter()

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
    query = db.query(Conversation).filter(Conversation.tenant_id == tenant.id)
    if user.role != "admin":
        # no rodízio cada vendedor só enxerga os leads que caíram pra ele; admin vê tudo
        query = query.join(Lead, Lead.id == Conversation.lead_id).filter(Lead.assigned_user_id == user.id)
    # um item por lead (um lead pode ter conversa em mais de um número), o mais recente primeiro
    conversations, seen = [], set()
    for conv in query.order_by(Conversation.last_message_at.desc()).all():
        if conv.lead_id not in seen:
            seen.add(conv.lead_id)
            conversations.append(conv)

    selected_lead = None
    messages = []
    active_conversation = None
    other_numbers = []
    if selected_lead_id:
        selected_lead = db.query(Lead).filter(Lead.id == selected_lead_id, Lead.tenant_id == tenant.id).first()
        if selected_lead and selected_lead.conversations:
            messages = selected_lead.all_messages
            active_conversation = selected_lead.active_conversation
            other_numbers = [
                n
                for n in db.query(WhatsAppNumber)
                .filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.is_active.is_(True))
                .order_by(WhatsAppNumber.label)
                .all()
                if n.id != active_conversation.whatsapp_number_id
                and (n.provider != "evolution" or n.connection_state == "open")
            ]

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
            "active_conversation": active_conversation,
            "other_numbers": other_numbers,
            "platform": resolve_platform(selected_lead.attribution) if selected_lead else None,
            "platform_label": PLATFORM_LABEL[resolve_platform(selected_lead.attribution)] if selected_lead else None,
        },
    )


@router.get("/media/{message_id}")
def get_media(
    message_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    message = db.get(Message, message_id)
    if not message or not message.media_id:
        raise HTTPException(status_code=404, detail="Mídia não encontrada")

    conversation = db.get(Conversation, message.conversation_id)
    if not conversation or conversation.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Mídia não encontrada")

    # 1º a cópia própria (sobrevive a número bloqueado); senão busca no WhatsApp
    content, mime_type = media_store.load(message.media_stored_key)
    if content is None:
        number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
        content, mime_type = messaging.fetch_media(number, message.media_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Mídia indisponível (expirou ou o número foi desconectado)")

    return Response(content=content, media_type=mime_type)


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

    conversation = lead.active_conversation
    number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
    ok, wa_id = messaging.send_text(number, lead.phone, body)
    _record_outbound(db, conversation, user, wa_id, body if ok else f"⚠️ Não enviada: {body}")

    return RedirectResponse(url=f"/inbox/{lead_id}", status_code=302)


@router.post("/leads/{lead_id}/switch-number")
def switch_number(
    lead_id: str,
    whatsapp_number_id: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Continuar a conversa por outro número (ex: o número do vendedor caiu ou
    foi bloqueado). O histórico continua o mesmo; só as próximas mensagens
    saem pelo número escolhido."""
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    number = db.get(WhatsAppNumber, whatsapp_number_id)
    if not lead or not number or number.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Lead ou número não encontrado")

    conversation = (
        db.query(Conversation)
        .filter(Conversation.lead_id == lead.id, Conversation.whatsapp_number_id == number.id)
        .first()
    )
    if not conversation:
        conversation = Conversation(
            tenant_id=tenant.id,
            lead_id=lead.id,
            whatsapp_number_id=number.id,
            assigned_user_id=lead.assigned_user_id,
        )
    conversation.last_message_at = datetime.datetime.utcnow()  # vira a conversa ativa
    db.add(conversation)
    db.commit()
    return RedirectResponse(url=f"/inbox/{lead_id}", status_code=302)


@router.post("/leads/{lead_id}/reply-media")
async def reply_lead_media(
    lead_id: str,
    file: UploadFile,
    caption: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Envia foto/vídeo/documento pro lead. Chamado via fetch() do Inbox (não
    é um form comum) pra dar pra mostrar 'enviando...' sem recarregar a
    página até a resposta da Meta confirmar."""
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead or not lead.conversations:
        return JSONResponse({"ok": False, "error": "conversa não encontrada"}, status_code=404)

    content = await file.read()
    max_bytes = 16 * 1024 * 1024  # limite da própria Cloud API pra imagem/doc (vídeo é maior, mas fica um teto seguro)
    if len(content) > max_bytes:
        return JSONResponse({"ok": False, "error": "arquivo maior que 16MB"}, status_code=400)

    conversation = lead.active_conversation
    number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
    mime_type = file.content_type or "application/octet-stream"
    media_kind = media_kind_for_mime(mime_type)

    ok, wa_id, media_id, error = messaging.send_media(
        number, lead.phone, content, file.filename or "arquivo", mime_type, media_kind, caption
    )
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=502)

    placeholder = {"image": "📷 Imagem", "video": "🎥 Vídeo", "audio": "🎤 Áudio", "document": "📄 Documento"}[media_kind]
    message = _record_outbound(
        db, conversation, user, wa_id, f"{placeholder}{' — ' + caption if caption else ''}", media_id, media_kind
    )
    stored_key = media_store.save(message.id, content, mime_type)
    if stored_key:
        message.media_stored_key = stored_key
        db.add(message)
        db.commit()
    return JSONResponse({"ok": True})


def _record_outbound(
    db: Session,
    conversation: Conversation,
    user: User,
    wa_id: str,
    body: str,
    media_id: str = "",
    media_type: str = "",
) -> Message:
    # no Evolution o webhook "fromMe" dessa mesma mensagem pode chegar antes
    # deste commit; aí ela já está salva e só marcamos quem enviou
    existing = db.query(Message).filter(Message.wa_message_id == wa_id).first() if wa_id else None
    message = existing or Message(conversation_id=conversation.id, direction="out", wa_message_id=wa_id)
    message.sender_user_id = user.id
    message.body = body
    message.media_id = media_id or message.media_id
    message.media_type = media_type or message.media_type
    conversation.last_message_at = datetime.datetime.utcnow()
    db.add(message)
    db.add(conversation)
    db.commit()
    return message


DATE_RANGE_LABELS = [
    ("all", "Todos"),
    ("today", "Hoje"),
    ("7d", "7 dias"),
    ("30d", "30 dias"),
    ("month", "Este mês"),
    ("custom", "Personalizado"),
]


def _range_from(now: datetime.datetime, date_range: str) -> Optional[datetime.datetime]:
    if date_range == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if date_range == "7d":
        return now - datetime.timedelta(days=7)
    if date_range == "30d":
        return now - datetime.timedelta(days=30)
    if date_range == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def _parse_date(value: str) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_view(
    request: Request,
    card: str = "all",
    stage_filter: str = "",
    platform_filter: str = "",
    q: str = "",
    date_range: str = "all",
    date_from: str = "",
    date_to: str = "",
    seller: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        seller = user.id  # vendedor só vê os próprios números
    sellers = (
        db.query(User).filter(User.tenant_id == tenant.id, User.pending_approval.is_(False)).order_by(User.name).all()
    )
    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()

    now = datetime.datetime.utcnow()
    range_to: Optional[datetime.datetime] = None
    if date_range == "custom":
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
        range_from = datetime.datetime.combine(parsed_from, datetime.time.min) if parsed_from else None
        range_to = datetime.datetime.combine(parsed_to, datetime.time.max) if parsed_to else None
    else:
        range_from = _range_from(now, date_range)

    leads_query = db.query(Lead).filter(Lead.tenant_id == tenant.id)
    if range_from:
        leads_query = leads_query.filter(Lead.created_at >= range_from)
    if range_to:
        leads_query = leads_query.filter(Lead.created_at <= range_to)
    all_period_leads = leads_query.all()
    leads = [lead for lead in all_period_leads if lead.assigned_user_id == seller] if seller else all_period_leads

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

    # funil lado a lado por vendedor (mesmo período; ignora o filtro de vendedor)
    seller_rows = []
    for s in sellers:
        mine = [lead for lead in all_period_leads if lead.assigned_user_id == s.id]
        if not mine and not s.is_active:
            continue
        won = [lead for lead in mine if lead.stage_id in won_stage_ids]
        seller_rows.append(
            {
                "user": s,
                "total": len(mine),
                "by_stage": {st.id: sum(1 for lead in mine if lead.stage_id == st.id) for st in stages},
                "won": len(won),
                "conversion": round(len(won) / len(mine) * 100) if mine else 0,
                "revenue": sum(lead.deal_value or 0 for lead in won),
            }
        )
    seller_rows.sort(key=lambda r: (r["won"], r["total"]), reverse=True)
    unassigned = sum(1 for lead in all_period_leads if not lead.assigned_user_id)

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

    # ---- tráfego: cruza leads (por campanha/conjunto/anúncio) com gasto importado ----
    qualified_stage_ids = {s.id for s in stages if s.conversion_event_name or s.is_won}
    spend_query = db.query(CampaignSpend).filter(CampaignSpend.tenant_id == tenant.id)
    if range_from:
        spend_query = spend_query.filter(CampaignSpend.date >= range_from.date())
    if range_to:
        spend_query = spend_query.filter(CampaignSpend.date <= range_to.date())
    spend_rows = spend_query.all()
    spend_by_key: dict[tuple, float] = {}
    for row in spend_rows:
        key = (row.platform, row.campaign, row.adset, row.ad)
        spend_by_key[key] = spend_by_key.get(key, 0.0) + row.spend
    has_spend_data = bool(spend_rows)

    traffic_groups: dict[tuple, dict] = {}
    for lead in leads:
        platform = resolve_platform(lead.attribution)
        campaign = lead.attribution.utm_campaign if lead.attribution else ""
        adset = lead.attribution.utm_term if lead.attribution else ""
        ad = lead.attribution.utm_content if lead.attribution else ""
        if not campaign:
            continue
        key = (platform, campaign, adset, ad)
        g = traffic_groups.setdefault(
            key,
            {
                "platform": PLATFORM_LABEL[platform],
                "campaign": campaign,
                "adset": adset,
                "ad": ad,
                "leads": 0,
                "qualified": 0,
                "won": 0,
                "revenue": 0.0,
            },
        )
        g["leads"] += 1
        if lead.stage_id in qualified_stage_ids:
            g["qualified"] += 1
        if lead.stage_id in won_stage_ids:
            g["won"] += 1
            g["revenue"] += lead.deal_value or 0

    traffic_rows = []
    for key, g in traffic_groups.items():
        spend = spend_by_key.get(key, 0.0)
        traffic_rows.append(
            {
                **g,
                "spend": spend,
                "cpl": (spend / g["leads"]) if g["leads"] else None,
                "cost_per_qualified": (spend / g["qualified"]) if g["qualified"] else None,
                "cac": (spend / g["won"]) if g["won"] else None,
                "ticket_medio": (g["revenue"] / g["won"]) if g["won"] else None,
            }
        )
    traffic_rows.sort(key=lambda r: r["leads"], reverse=True)

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
            "date_range": date_range,
            "date_from": date_from,
            "date_to": date_to,
            "date_range_labels": DATE_RANGE_LABELS,
            "seller": seller,
            "sellers": sellers,
            "seller_rows": seller_rows,
            "unassigned": unassigned,
            "traffic_rows": traffic_rows,
            "has_spend_data": has_spend_data,
        },
    )
