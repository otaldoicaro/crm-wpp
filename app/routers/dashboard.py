import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import CampaignSpend, Conversation, Lead, Message, PipelineStage, Tenant, User, WhatsAppNumber
from app.services import evolution_client, media_store, messaging
from app.services.conversions.dispatcher import dispatch_stage_conversion
from app.services.platform import PLATFORM_LABEL, resolve_platform
from app.services.messaging import media_kind_for_mime
from app.templating import templates
from app.timeutil import local_to_utc, to_local

router = APIRouter()

STAGE_COLOR_PALETTE = ["#4285F4", "#f2a71b", "#8b5cf6", "#22c55e", "#e21b3c", "#06b6d4", "#ec4899"]


def _stage_colors(stages: list[PipelineStage]) -> dict[str, str]:
    return {stage.id: STAGE_COLOR_PALETTE[i % len(STAGE_COLOR_PALETTE)] for i, stage in enumerate(stages)}


PIPELINE_COL_LIMIT = 30  # cartões por coluna; "ver mais" soma de 30 em 30


def _period_bounds(date_range: str, date_from: str, date_to: str):
    """(início, fim) em UTC pros filtros de período; None = sem limite."""
    if date_range == "custom":
        parsed_from, parsed_to = _parse_date(date_from), _parse_date(date_to)
        return (
            local_to_utc(datetime.datetime.combine(parsed_from, datetime.time.min)) if parsed_from else None,
            local_to_utc(datetime.datetime.combine(parsed_to, datetime.time.max)) if parsed_to else None,
        )
    return _range_from(datetime.datetime.utcnow(), date_range), None


@router.get("/", response_class=HTMLResponse)
def pipeline_view(
    request: Request,
    date_range: str = "30d",
    date_from: str = "",
    date_to: str = "",
    seller: str = "",
    col: str = "",
    n: int = PIPELINE_COL_LIMIT,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Quadro do Pipeline. Por padrão só leads que chegaram nos últimos 30 dias, sem
    arquivados, e no máximo 30 cartões por coluna (o total aparece no topo da coluna)."""
    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
    range_from, range_to = _period_bounds(date_range, date_from, date_to)

    base = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.archived_at.is_(None), Lead.deleted_at.is_(None))
    if range_from:
        base = base.filter(Lead.created_at >= range_from)
    if range_to:
        base = base.filter(Lead.created_at <= range_to)
    if seller:
        base = base.filter(Lead.assigned_user_id == seller)

    totals = dict(base.with_entities(Lead.stage_id, func.count(Lead.id)).group_by(Lead.stage_id).all())
    leads_by_stage: dict[str, list[Lead]] = {}
    for stage in stages:
        limit = max(n, PIPELINE_COL_LIMIT) if col == stage.id else PIPELINE_COL_LIMIT
        leads_by_stage[stage.id] = (
            base.filter(Lead.stage_id == stage.id).order_by(Lead.updated_at.desc()).limit(limit).all()
        )
    shown = [lead for col_leads in leads_by_stage.values() for lead in col_leads]
    lead_platforms = {lead.id: resolve_platform(lead.attribution) for lead in shown}
    archived_count = (
        db.query(func.count(Lead.id))
        .filter(Lead.tenant_id == tenant.id, Lead.archived_at.isnot(None), Lead.deleted_at.is_(None))
        .scalar()
    )

    return templates.TemplateResponse(
        request,
        "pipeline.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "pipeline",
            "stages": stages,
            "leads_by_stage": leads_by_stage,
            "totals": totals,
            "col_limit": PIPELINE_COL_LIMIT,
            "col": col,
            "n": n,
            "date_range": date_range,
            "date_from": date_from,
            "date_to": date_to,
            "date_range_labels": DATE_RANGE_LABELS,
            "seller": seller,
            "sellers": db.query(User)
            .filter(User.tenant_id == tenant.id, User.pending_approval.is_(False))
            .order_by(User.name)
            .all(),
            "archived_count": archived_count,
            "just_deleted": request.query_params.get("excluido") == "1",
            "stage_colors": _stage_colors(stages),
            "lead_platforms": lead_platforms,
            "platform_labels": PLATFORM_LABEL,
        },
    )


@router.get("/arquivados", response_class=HTMLResponse)
def archived_view(
    request: Request,
    busca: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.archived_at.isnot(None), Lead.deleted_at.is_(None))
    busca = busca.strip()
    if busca:
        digits = "".join(ch for ch in busca if ch.isdigit())
        conditions = [Lead.name.ilike(f"%{busca}%")]
        if digits:
            conditions.append(Lead.phone.like(f"%{digits}%"))
        query = query.filter(or_(*conditions))
    total = query.count()
    leads = query.order_by(Lead.archived_at.desc()).limit(200).all()
    stages = {s.id: s for s in db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id)}
    return templates.TemplateResponse(
        request,
        "arquivados.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "pipeline",
            "leads": leads,
            "total": total,
            "busca": busca,
            "stage_by_id": stages,
        },
    )


@router.post("/leads/{lead_id}/arquivo")
def toggle_archive(
    lead_id: str,
    voltar: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Arquiva (sai do Pipeline) ou restaura um lead manualmente."""
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    lead.archived_at = None if lead.archived_at else datetime.datetime.utcnow()
    db.add(lead)
    db.commit()
    return RedirectResponse(url=voltar if voltar.startswith("/") else "/", status_code=302)


@router.post("/leads/{lead_id}/excluir")
def delete_lead(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Manda o lead pra Lixeira (some de tudo). Fica registrado quem excluiu e quando."""
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    lead.deleted_at = datetime.datetime.utcnow()
    lead.deleted_by_user_id = user.id
    db.add(lead)
    db.commit()
    return RedirectResponse(url="/?excluido=1", status_code=302)


@router.post("/leads/excluir-em-massa")
def bulk_delete_leads(
    lead_ids: list[str] = Form(default=[]),
    voltar: str = Form("/dashboard"),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Admin: manda vários leads pra Lixeira de uma vez (cada um registra quem/quando)."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin pode excluir em massa")
    now = datetime.datetime.utcnow()
    count = (
        db.query(Lead)
        .filter(Lead.tenant_id == tenant.id, Lead.id.in_(lead_ids), Lead.deleted_at.is_(None))
        .update({Lead.deleted_at: now, Lead.deleted_by_user_id: user.id}, synchronize_session=False)
        if lead_ids
        else 0
    )
    db.commit()
    back = voltar if voltar.startswith("/") else "/dashboard"
    return RedirectResponse(url=f"{back}{'&' if '?' in back else '?'}excluidos={count}", status_code=302)


@router.get("/lixeira", response_class=HTMLResponse)
def trash_view(
    request: Request,
    busca: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin vê a Lixeira")
    query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.deleted_at.isnot(None))
    busca = busca.strip()
    if busca:
        digits = "".join(ch for ch in busca if ch.isdigit())
        conditions = [Lead.name.ilike(f"%{busca}%")]
        if digits:
            conditions.append(Lead.phone.like(f"%{digits}%"))
        query = query.filter(or_(*conditions))
    total = query.count()
    return templates.TemplateResponse(
        request,
        "lixeira.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "pipeline",
            "leads": query.order_by(Lead.deleted_at.desc()).limit(200).all(),
            "total": total,
            "busca": busca,
        },
    )


@router.post("/leads/{lead_id}/restaurar")
def restore_lead(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin pode restaurar da Lixeira")
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    lead.deleted_at = None
    db.add(lead)
    db.commit()
    return RedirectResponse(url="/lixeira?salvo=1", status_code=302)


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
            "won_stage_ids": [s.id for s in stages if s.is_won],
            "won_stage_name": next((s.name for s in stages if s.is_won), "Ganho"),
            "voltar": request.query_params.get("voltar", ""),
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
    voltar: str = Form(""),
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

    back = f"/inbox/{lead_id}" if voltar == "inbox" else f"/leads/{lead_id}"
    return RedirectResponse(url=f"{back}?salvo=1", status_code=302)


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


INBOX_LIST_LIMIT = 60  # com milhares de leads/mês a lista inteira pesaria; o resto se acha pela busca


def _inbox_context(
    db: Session,
    tenant: Tenant,
    user: User,
    selected_lead_id: Optional[str],
    busca: str = "",
    numero: str = "",
    vendedor: str = "",
) -> dict:
    query = (
        db.query(Conversation)
        .join(Lead, Lead.id == Conversation.lead_id)
        .filter(Conversation.tenant_id == tenant.id, Lead.deleted_at.is_(None))
    )
    if user.role != "admin":
        # no rodízio cada vendedor só enxerga os leads que caíram pra ele; admin vê tudo
        query = query.filter(Lead.assigned_user_id == user.id)
        numero = vendedor = ""
    if numero:
        query = query.filter(Conversation.whatsapp_number_id == numero)
    if vendedor == "sem":
        query = query.filter(Lead.assigned_user_id.is_(None))
    elif vendedor:
        query = query.filter(Lead.assigned_user_id == vendedor)
    busca = busca.strip()
    if busca:
        digits = "".join(ch for ch in busca if ch.isdigit())
        conditions = [Lead.name.ilike(f"%{busca}%")]
        if digits:
            conditions.append(Lead.phone.like(f"%{digits}%"))
        query = query.filter(or_(*conditions))
    # um item por lead (um lead pode ter conversa em mais de um número), o mais recente primeiro
    conversations, seen = [], set()
    for conv in query.order_by(Conversation.last_message_at.desc()).limit(INBOX_LIST_LIMIT * 2):
        if conv.lead_id not in seen and len(conversations) < INBOX_LIST_LIMIT:
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

    return {
            "list_sig": "|".join(f"{c.id}:{c.last_message_at.isoformat()}" for c in conversations),
            "list_truncated": len(conversations) >= INBOX_LIST_LIMIT,
            "busca": busca,
            "numero": numero,
            "vendedor": vendedor,
            "filter_numbers": db.query(WhatsAppNumber)
            .filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.is_active.is_(True))
            .order_by(WhatsAppNumber.label)
            .all()
            if user.role == "admin"
            else [],
            "filter_sellers": db.query(User)
            .filter(User.tenant_id == tenant.id, User.pending_approval.is_(False))
            .order_by(User.name)
            .all()
            if user.role == "admin"
            else [],
            "msg_sig": f"{len(messages)}:{messages[-1].id}:{messages[-1].body}" if messages else "0",
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
    }


INBOX_FILTER_COOKIE = "inbox_filtro"


def _render_inbox(request: Request, db: Session, tenant: Tenant, user: User, selected_lead_id: Optional[str]):
    # filtros de número/vendedor ficam lembrados (cookie) ao abrir conversa, responder, etc.
    qp = request.query_params
    changed = "numero" in qp or "vendedor" in qp
    if changed:
        numero, vendedor = qp.get("numero", ""), qp.get("vendedor", "")
    else:
        numero, _, vendedor = request.cookies.get(INBOX_FILTER_COOKIE, "|").partition("|")
    ctx = _inbox_context(db, tenant, user, selected_lead_id, qp.get("busca", ""), numero, vendedor)
    response = templates.TemplateResponse(request, "inbox.html", ctx)
    if changed:
        response.set_cookie(INBOX_FILTER_COOKIE, f"{numero}|{vendedor}", httponly=True, samesite="lax")
    return response


@router.get("/inbox-atualizar")
def inbox_refresh(
    lead: str = "",
    busca: str = "",
    numero: str = "",
    vendedor: str = "",
    ls: str = "",
    ms: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Chamado a cada poucos segundos pela tela do Inbox: devolve a lista e a
    conversa aberta já desenhadas, com uma "assinatura" pra tela só trocar o
    que mudou (mensagem nova do cliente ou enviada pelo celular)."""
    ctx = _inbox_context(db, tenant, user, lead or None, busca, numero, vendedor)
    if ctx["list_sig"] == ls and ctx["msg_sig"] == ms:
        return JSONResponse({"changed": False})  # nada novo: resposta mínima, sem desenhar nada
    return JSONResponse(
        {
            "list_sig": ctx["list_sig"],
            "list_html": templates.get_template("_inbox_list.html").render(ctx),
            "msg_sig": ctx["msg_sig"],
            "msg_html": templates.get_template("_inbox_messages.html").render(ctx) if lead else "",
        }
    )


AVATAR_TTL = datetime.timedelta(hours=24)


@router.get("/leads/{lead_id}/avatar")
def lead_avatar(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Foto de perfil do WhatsApp do lead. 404 = sem foto (a tela mostra a inicial)."""
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    conversation = lead.active_conversation if lead else None
    number = conversation.whatsapp_number if conversation else None
    if not lead or not number or number.provider != "evolution":
        raise HTTPException(status_code=404)

    def refresh_url() -> str:
        try:
            lead.avatar_url = evolution_client.profile_picture_url(number.evolution_instance, lead.phone)
        except evolution_client.EvolutionError:
            lead.avatar_url = ""
        lead.avatar_checked_at = datetime.datetime.utcnow()
        db.add(lead)
        db.commit()
        return lead.avatar_url

    now = datetime.datetime.utcnow()
    url = lead.avatar_url if lead.avatar_checked_at and now - lead.avatar_checked_at < AVATAR_TTL else refresh_url()
    for attempt in range(2):
        if not url:
            break
        try:
            resp = requests.get(url, timeout=8)
        except requests.RequestException:
            resp = None
        if resp is not None and resp.ok:
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "private, max-age=86400"},
            )
        url = refresh_url() if attempt == 0 else ""  # URL do WhatsApp expirou: pega uma nova
    raise HTTPException(status_code=404)


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
    conversation.last_preview = body[:200]
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
    """`now` em UTC; "Hoje"/"Este mês" começam à meia-noite de Brasília."""
    local_now = to_local(now)
    if date_range == "today":
        return local_to_utc(local_now.replace(hour=0, minute=0, second=0, microsecond=0))
    if date_range == "7d":
        return now - datetime.timedelta(days=7)
    if date_range == "30d":
        return now - datetime.timedelta(days=30)
    if date_range == "month":
        return local_to_utc(local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
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
    sellers = (
        db.query(User).filter(User.tenant_id == tenant.id, User.pending_approval.is_(False)).order_by(User.name).all()
    )
    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()

    now = datetime.datetime.utcnow()
    range_to: Optional[datetime.datetime] = None
    if date_range == "custom":
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
        range_from = local_to_utc(datetime.datetime.combine(parsed_from, datetime.time.min)) if parsed_from else None
        range_to = local_to_utc(datetime.datetime.combine(parsed_to, datetime.time.max)) if parsed_to else None
    else:
        range_from = _range_from(now, date_range)

    leads_query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None))
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
