import base64
import datetime
import os
import re
import threading
from urllib.parse import quote, unquote, urlencode
import time
from typing import Optional

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import CampaignSpend, Conversation, Lead, Message, PipelineStage, Tenant, User, WhatsAppNumber
from app.services import deals, evolution_client, followup, funnel, inbox_state, media_store, messaging, meta_spend, response_times, suggestions, traffic
from app.services.conversions.dispatcher import dispatch_stage_conversion
from app.routers.quick_replies import replies_for_js
from app.services.people import removed_user_ids, team_members
from app.services.platform import PLATFORM_LABEL, resolve_platform
from app.services.messaging import media_kind_for_mime
from app.templating import templates
from app.timeutil import local_to_utc, to_local

router = APIRouter()

STAGE_COLOR_PALETTE = ["#4285F4", "#f2a71b", "#8b5cf6", "#22c55e", "#e21b3c", "#06b6d4", "#ec4899"]


def _stage_colors(stages: list[PipelineStage]) -> dict[str, str]:
    return {stage.id: STAGE_COLOR_PALETTE[i % len(STAGE_COLOR_PALETTE)] for i, stage in enumerate(stages)}


# o que as telas de lista usam de cada lead, carregado em lote
LEAD_CARD_LOAD = (selectinload(Lead.attribution), selectinload(Lead.conversations), joinedload(Lead.assigned_user))

PIPELINE_COL_LIMIT = 30  # cartões por coluna; "ver mais" soma de 30 em 30


def _visible_lead(db: Session, tenant: Tenant, user: User, lead_id: str) -> Optional[Lead]:
    """Lead pelo id, se esta pessoa pode ver: admin vê todos; vendedor só os dele."""
    query = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id)
    if user.role != "admin":
        query = query.filter(Lead.assigned_user_id == user.id)
    return query.first()


def _period_bounds(date_range: str, date_from: str, date_to: str):
    """(início, fim) em UTC pros filtros de período; None = sem limite."""
    if date_range == "custom":
        parsed_from, parsed_to = _parse_date(date_from), _parse_date(date_to)
        return (
            local_to_utc(datetime.datetime.combine(parsed_from, datetime.time.min)) if parsed_from else None,
            local_to_utc(datetime.datetime.combine(parsed_to, datetime.time.max)) if parsed_to else None,
        )
    return _range_from(datetime.datetime.utcnow(), date_range), None


ORDER_LABELS = [("recentes", "Mais recentes primeiro"), ("espera", "Esperando há mais tempo primeiro")]


def _waiting_order() -> list:
    """Ordem "esperando há mais tempo": primeiro quem espera resposta nossa (do que espera há
    mais tempo pro mais novo), depois o resto. Mesma regra do "⏳ aguardando há X"."""
    never_answered = and_(Lead.first_response_at.is_(None), Lead.settled_at.is_(None))
    waiting = or_(never_answered, response_times.awaiting_reply_sql())
    since = case((Lead.first_response_at.is_(None), Lead.created_at), else_=Lead.last_inbound_at)
    return [case((waiting, 0), else_=1), since.asc()]


PIPE_ORDER_LABELS = [("recentes", "Mais recentes"), ("antigos", "Mais antigos"), ("espera", "Esperando há mais tempo")]
PIPE_COL_ORDER_COOKIE = "pipeline_ordem_col"


def _pipeline_order(mode: str) -> list:
    """Ordem dos cards numa coluna. "Recentes/antigos" = pela entrada do lead NESTA etapa."""
    entered = func.coalesce(Lead.stage_entered_at, Lead.created_at)
    if mode == "antigos":
        return [entered.asc()]
    if mode == "espera":
        return _waiting_order() + [entered.asc()]
    return [entered.desc()]


def _pipeline_col_orders(request: Request, stages: list) -> dict:
    """Ordem escolhida em cada coluna ({etapa: modo}); trocar a ordem geral zera as das colunas.
    ?oc=<etapa>:<modo> muda uma coluna (modo vazio = volta a seguir a geral)."""
    valid = dict(PIPE_ORDER_LABELS)
    if "ordem" in request.query_params:
        return {}
    orders = {}
    for item in request.cookies.get(PIPE_COL_ORDER_COOKIE, "").split(","):
        stage_id, _, mode = item.partition(":")
        if stage_id and mode in valid:
            orders[stage_id] = mode
    for item in request.query_params.getlist("oc"):
        stage_id, _, mode = item.partition(":")
        if mode in valid:
            orders[stage_id] = mode
        else:
            orders.pop(stage_id, None)
    ids = {st.id for st in stages}
    return {k: v for k, v in orders.items() if k in ids}


def _order_choice(request: Request, cookie: str) -> str:
    value = request.query_params.get("ordem", request.cookies.get(cookie, "recentes"))
    return value if value in dict(ORDER_LABELS) else "recentes"


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
    # filtros lembrados: voltar pelo menu "Pipeline" abre do jeito que a pessoa deixou
    qp = request.query_params
    if not any(k in qp for k in ("date_range", "date_from", "date_to", "seller", "atrasados")):
        saved = (request.cookies.get("pipeline_filtro", "") + "||||").split("|")
        date_range, date_from, date_to, seller = saved[0] or date_range, saved[1], saved[2], saved[3]
        atrasados_saved = saved[4] == "1"
    else:
        atrasados_saved = qp.get("atrasados") == "1"
    range_from, range_to = _period_bounds(date_range, date_from, date_to)
    ordem = request.query_params.get("ordem", request.cookies.get("pipeline_ordem", "recentes"))
    if ordem not in dict(PIPE_ORDER_LABELS):
        ordem = "recentes"
    col_orders = _pipeline_col_orders(request, stages)

    base = db.query(Lead).filter(
        Lead.tenant_id == tenant.id, Lead.archived_at.is_(None), Lead.deleted_at.is_(None), Lead.tag != "outro",
        Lead.is_group.is_(False),
    )
    if range_from:
        base = base.filter(Lead.created_at >= range_from)
    if range_to:
        base = base.filter(Lead.created_at <= range_to)
    if user.role != "admin":
        seller = user.id  # cada vendedor vê só o pipeline dele
    if seller:
        base = base.filter(Lead.assigned_user_id == seller)
    atrasados = atrasados_saved
    if atrasados:  # só quem está parado na etapa, com follow-up pendente ou próximo contato vencido
        base = base.filter(followup.late_condition(tenant, stages))

    totals = dict(base.with_entities(Lead.stage_id, func.count(Lead.id)).group_by(Lead.stage_id).all())
    leads_by_stage: dict[str, list[Lead]] = {}
    for stage in stages:
        limit = max(n, PIPELINE_COL_LIMIT) if col == stage.id else PIPELINE_COL_LIMIT
        leads_by_stage[stage.id] = (
            base.filter(Lead.stage_id == stage.id)
            .options(*LEAD_CARD_LOAD)  # carrega origem/conversas/atendente junto (sem 1 consulta por cartão)
            .order_by(*_pipeline_order(col_orders.get(stage.id, ordem)))
            .limit(limit)
            .all()
        )
    shown = [lead for col_leads in leads_by_stage.values() for lead in col_leads]
    lead_platforms = {lead.id: resolve_platform(lead.attribution) for lead in shown}
    archived_count = (
        db.query(func.count(Lead.id))
        .filter(Lead.tenant_id == tenant.id, Lead.archived_at.isnot(None), Lead.deleted_at.is_(None))
        .scalar()
    )

    response = templates.TemplateResponse(
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
            "sellers": team_members(db, tenant.id),
            "archived_count": archived_count,
            "just_deleted": request.query_params.get("excluido") == "1",
            "stage_colors": _stage_colors(stages),
            "lead_platforms": lead_platforms,
            "platform_labels": PLATFORM_LABEL,
            "ordem": ordem,
            "order_labels": PIPE_ORDER_LABELS,
            "col_orders": col_orders,
            "atrasados": atrasados,
        },
    )
    response.set_cookie("pipeline_ordem", ordem, httponly=True, samesite="lax")
    response.set_cookie(PIPE_COL_ORDER_COOKIE, ",".join(f"{k}:{v}" for k, v in col_orders.items()), httponly=True, samesite="lax")
    response.set_cookie(
        "pipeline_filtro", f"{date_range}|{date_from}|{date_to}|{seller if user.role == 'admin' else ''}|{'1' if atrasados else ''}",
        httponly=True, samesite="lax",
    )
    return response


@router.get("/arquivados", response_class=HTMLResponse)
def archived_view(
    request: Request,
    busca: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.archived_at.isnot(None), Lead.deleted_at.is_(None))
    if user.role != "admin":
        query = query.filter(Lead.assigned_user_id == user.id)
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
    lead = _visible_lead(db, tenant, user, lead_id)
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
    lead = _visible_lead(db, tenant, user, lead_id)
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
    lead = _visible_lead(db, tenant, user, lead_id)
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
    lead = _visible_lead(db, tenant, user, lead_id)
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
            "people": team_members(db, tenant.id, only_active=True) if user.role == "admin" else [],
            "start_numbers": _start_numbers(db, tenant, user, lead),
            "wa_link": _wa_link(lead),
            "deal_info": None if lead.is_group else deals.summary(db, lead),
            "start_error": request.query_params.get("erro_envio", ""),
        },
    )


def _start_numbers(db: Session, tenant: Tenant, user: User, lead: Lead) -> list:
    """WhatsApps conectados que podem mandar a 1ª mensagem: o do vendedor do lead primeiro.
    Vendedor só usa o(s) dele; admin escolhe qualquer um."""
    numbers = [
        n for n in db.query(WhatsAppNumber).filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.is_active.is_(True))
        .order_by(WhatsAppNumber.label)
        if n.provider != "evolution" or n.connection_state == "open"
    ]
    if user.role != "admin":
        numbers = [n for n in numbers if n.owner_user_id == user.id]
    numbers.sort(key=lambda n: n.owner_user_id != lead.assigned_user_id)
    return numbers


def _wa_link(lead: Lead) -> str:
    digits = "".join(ch for ch in (lead.phone or "") if ch.isdigit())
    if not digits or lead.is_group:
        return ""
    first = (lead.name or "").split(" ")[0]
    return f"https://wa.me/{digits}?" + urlencode({"text": f"Olá{', ' + first if first else ''}! Tudo bem?"})


@router.post("/leads/{lead_id}/iniciar")
def start_conversation(
    lead_id: str,
    whatsapp_number_id: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Manda a 1ª mensagem pro lead (ex: preencheu o formulário/clicou no link mas nunca chamou
    no WhatsApp). A conversa passa a existir e aparece no Inbox."""
    lead = _lead_for_action(db, tenant, user, lead_id)
    number = next((n for n in _start_numbers(db, tenant, user, lead) if n.id == whatsapp_number_id), None)
    if number is None or not body.strip():
        raise HTTPException(status_code=400, detail="Escolha um WhatsApp conectado e escreva a mensagem")
    ok, wa_id, secret = messaging.send_text(number, lead.phone, body.strip())
    if not ok:
        return RedirectResponse(
            url=f"/leads/{lead_id}?erro_envio=" + quote("O WhatsApp " + number.label + " não conseguiu enviar. Confira se o número do lead está certo e se o WhatsApp está conectado."),
            status_code=302,
        )
    # consulta de novo: o aviso do WhatsApp sobre essa mesma mensagem pode já ter criado a conversa
    conversation = (
        db.query(Conversation).filter(Conversation.lead_id == lead.id, Conversation.whatsapp_number_id == number.id).first()
    )
    if conversation is None:
        conversation = Conversation(tenant_id=tenant.id, lead_id=lead.id, whatsapp_number_id=number.id, assigned_user_id=lead.assigned_user_id)
        db.add(conversation)
        db.flush()
    response_times.mark_outbound(lead)
    _record_outbound(db, conversation, user, wa_id, body.strip(), secret=secret)
    return RedirectResponse(url=f"/inbox/{lead_id}", status_code=302)


@router.post("/leads/{lead_id}/update")
def update_lead(
    lead_id: str,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    stage_id: str = Form(...),
    deal_value: str = Form(""),
    loss_reason: str = Form(""),
    loss_detail: str = Form(""),
    voltar: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = _visible_lead(db, tenant, user, lead_id)
    stage = db.query(PipelineStage).filter(PipelineStage.id == stage_id, PipelineStage.tenant_id == tenant.id).first()
    if not lead:
        return RedirectResponse(url="/", status_code=302)

    stage_changed = stage and lead.stage_id != stage.id
    if stage_changed and funnel.is_automatic(stage):
        return RedirectResponse(url=f"/leads/{lead_id}?erro=" + quote(funnel.AUTOMATIC_MSG), status_code=302)
    loss_error = funnel.check_loss(db, lead, stage, loss_reason) if stage and stage.is_lost and (stage_changed or loss_reason != lead.loss_reason) else None
    if loss_error:
        return RedirectResponse(url=f"/leads/{lead_id}?erro=" + quote(loss_error), status_code=302)
    lead.name = name
    lead.phone = phone
    lead.email = email
    if stage and stage.is_lost:
        lead.loss_reason, lead.loss_detail = loss_reason, loss_detail.strip()
    try:
        lead.deal_value = float(deal_value) if deal_value else None
    except ValueError:
        lead.deal_value = None
    if stage:
        funnel.set_stage(lead, stage)
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
    loss_reason: str = Form(""),
    loss_detail: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = _visible_lead(db, tenant, user, lead_id)
    stage = db.query(PipelineStage).filter(PipelineStage.id == stage_id, PipelineStage.tenant_id == tenant.id).first()
    if lead and stage and lead.stage_id != stage.id and funnel.is_automatic(stage):
        if ajax:
            return JSONResponse({"ok": False, "error": funnel.AUTOMATIC_MSG}, status_code=400)
        return RedirectResponse(url="/?erro=" + quote(funnel.AUTOMATIC_MSG), status_code=302)
    loss_error = funnel.check_loss(db, lead, stage, loss_reason) if lead and stage and lead.stage_id != stage.id else None
    if loss_error:
        if ajax:
            return JSONResponse({"ok": False, "error": loss_error}, status_code=400)
        return RedirectResponse(url="/?erro=" + quote(loss_error), status_code=302)
    if lead and stage:
        funnel.set_stage(lead, stage)
        if stage.is_lost:
            lead.loss_reason, lead.loss_detail = loss_reason, loss_detail.strip()
        db.add(lead)
        db.commit()
        if stage.conversion_event_name:
            dispatch_stage_conversion(db, lead, stage.conversion_event_name)
    if ajax:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=302)


@router.get("/dashboard/campanha", response_class=HTMLResponse)
def campaign_leads(
    request: Request,
    c: str = "",
    s: str = "",
    a: str = "",
    date_range: str = "30d",
    date_from: str = "",
    date_to: str = "",
    seller: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Os leads de uma campanha (ou conjunto/anúncio) do Dashboard, com a etapa de cada um;
    clicando, a conversa abre do lado pra ver se o lead é qualificado de verdade."""
    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
    range_from, range_to = _period_bounds(date_range, date_from, date_to)
    query = db.query(Lead).filter(
        Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None), Lead.tag != "outro", Lead.is_group.is_(False)
    )
    if range_from:
        query = query.filter(Lead.created_at >= range_from)
    if range_to:
        query = query.filter(Lead.created_at <= range_to)
    if user.role != "admin":
        seller = user.id  # vendedor vê só os leads dele de cada campanha
    if seller:
        query = query.filter(Lead.assigned_user_id == seller)
    leads = query.options(*LEAD_CARD_LOAD).all()
    tree = traffic.build(
        db, tenant.id, leads,
        to_local(range_from).date() if range_from else None, to_local(range_to).date() if range_to else None,
        funnel.is_qualified(stages), {st.id for st in stages if st.is_won},
    )
    trail = traffic.find(tree, c, s, a)
    if trail is None:
        raise HTTPException(status_code=404, detail="Campanha não encontrada nesse período")
    node = trail[-1]
    wanted = set(node["lead_ids"])
    chosen = sorted((lead for lead in leads if lead.id in wanted), key=lambda lead: lead.created_at, reverse=True)
    stage_by_id = {st.id: st for st in stages}
    return templates.TemplateResponse(
        request,
        "campanha_leads.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "dashboard",
            "trail": trail,
            "node": node,
            "leads": chosen,
            "stages": stages,
            "stage_by_id": stage_by_id,
            "stage_counts": {st.id: sum(1 for lead in chosen if lead.stage_id == st.id) for st in stages},
            "back": "/dashboard?" + urlencode({"aba": "trafego", "date_range": date_range, "date_from": date_from, "date_to": date_to, "seller": seller}) + "#trafego",
        },
    )


@router.get("/prazos", response_class=HTMLResponse)
def deadlines_page(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Prazos do follow-up (por cliente): horas em cada etapa, horas pro follow-up e se o
    próximo contato é obrigatório."""
    if user.role != "admin":
        raise HTTPException(status_code=403)
    return templates.TemplateResponse(request, "prazos.html", {
        "tenant": tenant, "user": user, "active_nav": "pipeline", "stages": funnel._stages(db, tenant.id),
    })


@router.post("/prazos")
async def save_deadlines(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        raise HTTPException(status_code=403)
    form = await request.form()

    def hours(name: str, default: int = 0) -> int:
        try:
            return max(0, min(24 * 90, int(float(str(form.get(name, default)).replace(",", ".")))))
        except ValueError:
            return default

    def apply():
        for st in funnel._stages(db, tenant.id):
            if f"sla_{st.id}" in form:
                st.sla_hours = hours(f"sla_{st.id}")
        tenant.followup_hours = hours("followup_hours", 24) or 24
        tenant.require_next_step = form.get("require_next_step") == "1"
        db.commit()

    await run_in_threadpool(apply)
    return RedirectResponse(url="/prazos?salvo=1", status_code=302)


@router.post("/leads/{lead_id}/proximo-contato")
def set_next_step(
    lead_id: str,
    quando: str = Form(""),
    nota: str = Form(""),
    voltar: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """📅 Próximo contato agendado (data/hora local)."""
    lead = _lead_for_action(db, tenant, user, lead_id)
    try:
        lead.next_action_at = local_to_utc(datetime.datetime.fromisoformat(quando)) if quando else None
    except ValueError:
        return RedirectResponse(url=f"/leads/{lead_id}?erro=" + quote("Data inválida."), status_code=302)
    lead.next_action_note = nota.strip()[:255]
    db.commit()
    return RedirectResponse(url=voltar if voltar.startswith("/") else f"/inbox/{lead_id}", status_code=302)


@router.post("/dashboard/meta-contas")
def set_meta_accounts(
    contas: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Contas de anúncio do Meta deste cliente (normalmente detectadas sozinhas)."""
    if user.role != "admin":
        raise HTTPException(status_code=403)
    ids = [p for p in re.split(r"[\s,;]+", contas.replace("act_", "")) if p.isdigit()]
    tenant.meta_ad_accounts = ",".join(dict.fromkeys(ids))
    db.commit()
    threading.Thread(target=meta_spend.sync_all, kwargs={"days": meta_spend.FIRST_SYNC_DAYS}, daemon=True).start()
    return RedirectResponse(url="/dashboard?aba=trafego&salvo=1#trafego", status_code=302)


SEARCH_LIMIT = 100


def _text_search(db: Session, tenant: Tenant, user: User, q: str, vendedor: str = "", per_lead: bool = False) -> list:
    """Mensagens com esse texto (as mais recentes primeiro), [(Message, Lead)]. Vendedor só nas
    conversas dele. per_lead=True: uma por conversa (a mais recente que tem o texto)."""
    query = (
        db.query(Message, Lead)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Lead, Lead.id == Conversation.lead_id)
        .filter(
            Conversation.tenant_id == tenant.id, Lead.deleted_at.is_(None),
            Message.direction != "note", Message.body.ilike(f"%{q}%"),
        )
    )
    if user.role != "admin":
        query = query.filter(Lead.assigned_user_id == user.id)
    elif vendedor == "sem":
        query = query.filter(Lead.assigned_user_id.is_(None))
    elif vendedor and vendedor != "removidos":
        query = query.filter(Lead.assigned_user_id == vendedor)
    rows = query.order_by(Message.created_at.desc()).limit(SEARCH_LIMIT * (4 if per_lead else 1)).all()
    if not per_lead:
        return rows
    seen, out = set(), []
    for msg, lead in rows:
        if lead.id not in seen:
            seen.add(lead.id)
            out.append((msg, lead))
    return out[:SEARCH_LIMIT]


@router.get("/busca", response_class=HTMLResponse)
def message_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Procura um texto dentro das mensagens (as mais recentes primeiro). Vendedor só acha
    nas conversas dele. Usa o índice de trigramas do Postgres (db.setup_text_search)."""
    q = q.strip()[:100]
    hits = _text_search(db, tenant, user, q) if len(q) >= 3 else []
    return templates.TemplateResponse(request, "busca.html", {
        "tenant": tenant, "user": user, "active_nav": "inbox", "q": q, "hits": hits, "limit": SEARCH_LIMIT,
    })


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


INBOX_LIST_LIMIT = 60  # com milhares de leads/mês a lista inteira pesaria; "Ver mais" traz mais 60 por clique
INBOX_LIST_MAX = 1000


def _list_limit(raw, step: int = INBOX_LIST_LIMIT) -> int:
    """Quantas conversas mostrar (cresce com o "Ver mais"; sempre múltiplo do passo, com teto)."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return step
    return max(step, min(INBOX_LIST_MAX, value - value % step))


INBOX_CONTACT_TYPES = [
    ("semgrupos", "Leads e clientes"),
    ("leads", "Só leads"),
    ("cliente", "Só clientes"),
    ("outro", "Outros (não é venda)"),
    ("grupos", "👥 Grupos"),
    ("favoritas", "★ Favoritas"),
    ("", "Tudo (com grupos)"),
]
INBOX_STATUSES = [  # "precisa de atenção": um de cada vez
    ("naolidas", "Não lidas"),
    ("naorespondidas", "Não respondidas"),
    ("followup", "📞 Follow-up"),
    ("parados", "⏰ Parados"),
]
LEGACY_STATUS_TIPOS = ("naolidas", "followup", "parados")  # antes ficavam junto com os tipos


def _inbox_query(
    db: Session, tenant: Tenant, user: User, busca: str, numero: str, vendedor: str, tipo: str = "", lidas: str = "",
    etapa: str = "",
):
    """Conversas visíveis no Inbox com os filtros aplicados. Devolve (query, removed_ids).
    tipo = quais contatos (INBOX_CONTACT_TYPES); lidas = situação (INBOX_STATUSES);
    etapa = etapa do funil."""
    query = (
        db.query(Conversation)
        .join(Lead, Lead.id == Conversation.lead_id)
        .filter(Conversation.tenant_id == tenant.id, Lead.deleted_at.is_(None))
    )
    if user.role != "admin":
        # no rodízio cada vendedor só enxerga os leads que caíram pra ele; admin vê tudo
        query = query.filter(Lead.assigned_user_id == user.id)
    if numero:
        query = query.filter(Conversation.whatsapp_number_id == numero)
    removed_ids = removed_user_ids(db, tenant.id)
    if vendedor == "sem":
        query = query.filter(Lead.assigned_user_id.is_(None))
    elif vendedor == "removidos":
        query = query.filter(Lead.assigned_user_id.in_(removed_ids))
    elif vendedor:
        query = query.filter(Lead.assigned_user_id == vendedor)
    elif removed_ids:  # "Toda a equipe" = só quem está na equipe hoje (e leads sem vendedor)
        query = query.filter(or_(Lead.assigned_user_id.is_(None), Lead.assigned_user_id.notin_(removed_ids)))
    status = lidas or (tipo if tipo in LEGACY_STATUS_TIPOS else "")
    if status in ("nao", "naorespondidas"):
        query = inbox_state.unanswered_filter(query)
    elif status == "naolidas":
        query = inbox_state.unread_filter(query, user)
    elif status == "followup":
        query = query.filter(followup.followup_condition(tenant, funnel._stages(db, tenant.id)))
    elif status == "parados":
        query = query.filter(followup.stale_condition(funnel._stages(db, tenant.id)))
    if etapa:
        query = query.filter(Lead.stage_id == etapa)
    if tipo == "favoritas":
        query = inbox_state.favorite_filter(query, user)
    elif tipo == "grupos":
        query = query.filter(Lead.is_group.is_(True))
    elif tipo == "semgrupos":
        query = query.filter(Lead.is_group.is_(False))
    elif tipo == "leads":
        query = query.filter(Lead.tag == "", Lead.is_group.is_(False))
    elif tipo in ("cliente", "outro"):
        query = query.filter(Lead.tag == tipo)
    busca = busca.strip()
    if busca:
        digits = "".join(ch for ch in busca if ch.isdigit())
        conditions = [Lead.name.ilike(f"%{busca}%")]
        if digits:
            conditions.append(Lead.phone.like(f"%{digits}%"))
        query = query.filter(or_(*conditions))
    return query, removed_ids


def _inbox_order(ordem: str) -> list:
    if ordem == "espera":
        return _waiting_order() + [Conversation.last_message_at.desc()]
    return [Conversation.last_message_at.desc()]


def _first_per_lead(rows, limit: int = INBOX_LIST_LIMIT) -> list:
    """Um item por lead (um lead pode ter conversa em mais de um número), o mais recente primeiro."""
    picked, seen = [], set()
    for row in rows:
        if row.lead_id not in seen and len(picked) < limit:
            seen.add(row.lead_id)
            picked.append(row)
    return picked


def _list_sig(rows) -> str:
    return "|".join(f"{r.id}:{r.last_message_at.isoformat()}" for r in rows)


def _msg_sig(db: Session, lead_id: Optional[str]) -> str:
    """Muda quando chega/sai mensagem ou quando uma é editada/apagada (o texto muda de tamanho)."""
    if not lead_id:
        return "0"
    count, last, size = (
        db.query(func.count(Message.id), func.max(Message.created_at), func.sum(func.length(Message.body)))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id == lead_id)
        .one()
    )
    return f"{count}:{last.isoformat() if last else ''}:{size or 0}"


def _inbox_context(
    db: Session,
    tenant: Tenant,
    user: User,
    selected_lead_id: Optional[str],
    busca: str = "",
    numero: str = "",
    vendedor: str = "",
    tipo: str = "",
    lidas: str = "",
    limit: int = INBOX_LIST_LIMIT,
    ordem: str = "recentes",
    etapa: str = "",
) -> dict:
    if user.role != "admin":
        numero = vendedor = ""
    query, removed_ids = _inbox_query(db, tenant, user, busca, numero, vendedor, tipo, lidas, etapa)

    def count(status: str) -> int:  # quantas em cada situação, com os outros filtros atuais
        q, _ = _inbox_query(db, tenant, user, "", numero, vendedor, tipo, status, etapa)
        return q.with_entities(func.count(func.distinct(Conversation.lead_id))).scalar() or 0

    status_counts = {key: count(key) for key, _ in INBOX_STATUSES}
    unread_total, naolidas_total = status_counts["naorespondidas"], status_counts["naolidas"]
    conversations = _first_per_lead(
        query.options(joinedload(Conversation.lead).joinedload(Lead.assigned_user))  # sem 1 consulta por item
        .order_by(*_inbox_order(ordem))
        .limit(limit * 2),
        limit,
    )

    selected_lead = None
    messages = []
    active_conversation = None
    other_numbers = []
    if selected_lead_id:
        selected_lead = db.query(Lead).filter(Lead.id == selected_lead_id, Lead.tenant_id == tenant.id).first()
        if selected_lead and user.role != "admin" and selected_lead.assigned_user_id != user.id:
            selected_lead = None  # vendedor só abre conversa de lead dele (ex: link antigo de um lead transferido)
        if selected_lead and selected_lead.conversations:
            messages = selected_lead.all_messages
            active_conversation = selected_lead.active_conversation
            other_numbers = [] if user.role != "admin" else [
                n
                for n in db.query(WhatsAppNumber)
                .filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.is_active.is_(True))
                .order_by(WhatsAppNumber.label)
                .all()
                if n.id != active_conversation.whatsapp_number_id
                and (n.provider != "evolution" or n.connection_state == "open")
            ]

    return {
            "list_sig": _list_sig(conversations),
            "unread": inbox_state.unread_map(db, user, [c.lead_id for c in conversations]),
            "tipo": tipo,
            "lidas": lidas,
            "unread_total": unread_total,
            "naolidas_total": naolidas_total,
            "favorites": inbox_state.favorite_ids(db, user, [c.lead_id for c in conversations]),
            "tags": inbox_state.TAGS,
            "tag_label": inbox_state.TAG_LABEL,
            "list_truncated": len(conversations) >= limit and limit < INBOX_LIST_MAX,
            "list_total": query.with_entities(func.count(func.distinct(Conversation.lead_id))).scalar() or 0,
            "list_limit": limit,
            "list_step": INBOX_LIST_LIMIT,
            "ordem": ordem,
            "order_labels": ORDER_LABELS,
            "etapa": etapa,
            "status_counts": status_counts,
            "contact_types": INBOX_CONTACT_TYPES,
            "statuses": INBOX_STATUSES,
            "filter_stages": funnel._stages(db, tenant.id),
            "busca": busca,
            "numero": numero,
            "vendedor": vendedor,
            "filter_numbers": db.query(WhatsAppNumber)
            .filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.is_active.is_(True))
            .order_by(WhatsAppNumber.label)
            .all()
            if user.role == "admin"
            else [],
            "filter_sellers": [p for p in team_members(db, tenant.id) if p.role == "agent"] if user.role == "admin" else [],
            "has_removed": bool(removed_ids) if user.role == "admin" else False,
            "msg_sig": _msg_sig(db, selected_lead.id) if selected_lead else "0",
            "tenant": tenant,
            "user": user,
            "active_nav": "inbox",
            "conversations": conversations,
            "selected_lead": selected_lead,
            "messages": messages,
            "active_conversation": active_conversation,
            "other_numbers": other_numbers,
            "platform": resolve_platform(selected_lead.attribution) if selected_lead else None,
            "transfer_people": _transfer_options(db, tenant, selected_lead)
            if selected_lead and not selected_lead.is_group and user.role == "admin" else [],
            "stages": db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
            if selected_lead and not selected_lead.is_group else [],
            "deal_info": deals.summary(db, selected_lead) if selected_lead and not selected_lead.is_group else None,
            "can_settle": bool(selected_lead and not selected_lead.is_group and response_times.waiting_since(selected_lead)),
            "platform_label": PLATFORM_LABEL[resolve_platform(selected_lead.attribution)] if selected_lead else None,
    }


def _transfer_options(db: Session, tenant: Tenant, lead: Lead) -> list:
    """Pra quem dá pra transferir: equipe ativa (menos quem já atende), com o WhatsApp
    conectado de cada um (pra oferecer "continuar pelo número dele")."""
    numbers = {
        n.owner_user_id: n
        for n in db.query(WhatsAppNumber).filter(
            WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.is_active.is_(True), WhatsAppNumber.owner_user_id.isnot(None)
        )
        if n.provider != "evolution" or n.connection_state == "open"
    }
    current = lead.active_conversation.whatsapp_number_id if lead.active_conversation else None
    return [
        {"user": p, "number": numbers[p.id] if p.id in numbers and numbers[p.id].id != current else None}
        for p in team_members(db, tenant.id, only_active=True)
        if p.id != lead.assigned_user_id
    ]


INBOX_FILTER_COOKIE = "inbox_filtro"
INBOX_TEXT_COOKIE = "inbox_texto"


def _render_inbox(request: Request, db: Session, tenant: Tenant, user: User, selected_lead_id: Optional[str]):
    # filtros de número/vendedor ficam lembrados (cookie) ao abrir conversa, responder, etc.
    qp = request.query_params
    cookie = request.cookies.get(INBOX_FILTER_COOKIE)
    saved = ((cookie if cookie is not None else "||semgrupos|||") + "|||||").split("|")  # padrão: sem grupos
    numero = ""  # filtro por número saiu do Inbox: o vendedor já é o dono do número
    vendedor = qp.get("vendedor", saved[1]) if "numero" in qp or "vendedor" in qp else saved[1]
    tipo = qp.get("tipo", saved[2])
    lidas = qp.get("lidas", saved[3])
    etapa = qp.get("etapa", saved[5])
    # filtros de antes (situação misturada nos tipos; "nao" = não respondidas)
    if tipo in LEGACY_STATUS_TIPOS:
        lidas, tipo = tipo, "semgrupos"
    if lidas == "nao":
        lidas = "naorespondidas"
    if etapa and not db.query(PipelineStage.id).filter(PipelineStage.id == etapa, PipelineStage.tenant_id == tenant.id).first():
        etapa = ""
    ordem = qp.get("ordem", saved[4]) if qp.get("ordem", saved[4]) in dict(ORDER_LABELS) else "recentes"
    # filtro lembrado de um vendedor/número que foi excluído depois: volta pra "todos"
    if vendedor not in ("", "sem", "removidos") and not db.query(User.id).filter(
        User.id == vendedor, User.tenant_id == tenant.id, User.role == "agent"
    ).first():
        vendedor = ""  # vendedor excluído, ou um admin (o filtro mostra só vendedores)
    if numero and not db.query(WhatsAppNumber.id).filter(WhatsAppNumber.id == numero, WhatsAppNumber.tenant_id == tenant.id).first():
        numero = ""
    inbox_state.ensure_baseline(db, user)
    if selected_lead_id:
        inbox_state.mark_read(db, user.id, selected_lead_id)
    ctx = _inbox_context(
        db, tenant, user, selected_lead_id, qp.get("busca", ""), numero, vendedor, tipo, lidas, _list_limit(qp.get("qtd")), ordem, etapa
    )
    ctx["quick_replies"] = replies_for_js(db, tenant, user)
    # 🔎 busca no texto das conversas: a lista da esquerda vira os resultados e continua ali
    # enquanto a pessoa abre cada conversa (o texto vai no link); some ao apagar o texto
    # lembrada num cookie de sessão: responder/trocar filtro não perde a busca; sair do Inbox
    # (outra aba do menu) apaga o cookie (base.html), e apagar o texto também
    texto = (qp["texto"] if "texto" in qp else unquote(request.cookies.get(INBOX_TEXT_COOKIE, ""))).strip()[:100]
    ctx["texto"] = texto
    ctx["text_hits"] = _text_search(db, tenant, user, texto, vendedor, per_lead=True) if len(texto) >= 3 else None
    response = templates.TemplateResponse(request, "inbox.html", ctx)
    if len(texto) >= 3:
        response.set_cookie(INBOX_TEXT_COOKIE, quote(texto), samesite="lax")  # sem httponly: o menu apaga
    else:
        response.delete_cookie(INBOX_TEXT_COOKIE)
    response.set_cookie(INBOX_FILTER_COOKIE, f"{numero}|{vendedor}|{tipo}|{lidas}|{ordem}|{etapa}", httponly=True, samesite="lax")
    return response


@router.post("/inbox/{lead_id}/lida")
def inbox_mark_read(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if _visible_lead(db, tenant, user, lead_id):
        inbox_state.mark_read(db, user.id, lead_id)
    return JSONResponse({"ok": True})


@router.post("/inbox/{lead_id}/favorita")
def inbox_favorite(
    lead_id: str,
    valor: str = Form("1"),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if _visible_lead(db, tenant, user, lead_id):
        inbox_state.set_favorite(db, user.id, lead_id, valor == "1")
    return JSONResponse({"ok": True})


def _lead_for_action(db: Session, tenant: Tenant, user: User, lead_id: str) -> Lead:
    """Lead que esta pessoa pode mexer: admin mexe em todos, vendedor só nos dele."""
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None)).first()
    if not lead or (user.role != "admin" and lead.assigned_user_id != user.id):
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    return lead


def _add_note(db: Session, lead: Lead, text: str) -> None:
    """Aviso no meio da conversa (só no CRM, o cliente não recebe): transferência, encerramento."""
    conversation = lead.active_conversation
    if conversation is not None:
        db.add(Message(conversation_id=conversation.id, direction="note", body=text))


def _parse_brl(text: str) -> float:
    """'10.000,50' / '10000,5' / '10.000' / '1250.90' -> número. Vírgula = centavos;
    ponto seguido de 3 dígitos = milhar."""
    text = (text or "").strip().replace("R$", "").replace(" ", "")
    if not text:
        return 0.0
    if "," in text:
        return float(text.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        return float(text.replace(".", ""))
    return float(text)


@router.post("/leads/{lead_id}/etapa")
def set_lead_stage(
    lead_id: str,
    stage_id: str = Form(...),
    deal_value: str = Form(""),
    loss_reason: str = Form(""),
    loss_detail: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Etapa do funil + valor da venda direto do topo da conversa (sem abrir a ficha)."""
    lead = _lead_for_action(db, tenant, user, lead_id)
    stage = db.query(PipelineStage).filter(PipelineStage.id == stage_id, PipelineStage.tenant_id == tenant.id).first()
    if stage is None:
        return JSONResponse({"ok": False, "error": "etapa não encontrada"}, status_code=404)
    try:
        value = _parse_brl(deal_value)
    except ValueError:
        return JSONResponse({"ok": False, "error": "valor inválido (use só números, ex: 1250,90)"}, status_code=400)
    if stage.is_won and value <= 0:
        return JSONResponse({"ok": False, "error": f"pra marcar como {stage.name}, preencha o valor da venda"}, status_code=400)
    changed = lead.stage_id != stage.id
    if changed and funnel.is_automatic(stage):
        return JSONResponse({"ok": False, "error": funnel.AUTOMATIC_MSG}, status_code=400)
    loss_error = funnel.check_loss(db, lead, stage, loss_reason) if changed or loss_reason != lead.loss_reason else None
    if loss_error:
        return JSONResponse({"ok": False, "error": loss_error}, status_code=400)
    funnel.set_stage(lead, stage)
    if value > 0:
        lead.deal_value = value
    if stage.is_lost:
        lead.loss_reason, lead.loss_detail = loss_reason, loss_detail.strip()
    db.commit()
    if changed and stage.conversion_event_name:
        dispatch_stage_conversion(db, lead, stage.conversion_event_name)
    return JSONResponse({"ok": True})


@router.post("/leads/{lead_id}/novo-negocio")
def new_deal(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """"+ Novo negócio": o cliente que já fechou (ganho/perdido) quer comprar de novo antes dos
    7 dias em que o CRM abriria sozinho. A conversa passa pro negócio novo."""
    lead = _lead_for_action(db, tenant, user, lead_id)
    if not deals.is_closed(lead):
        return RedirectResponse(url=f"/leads/{lead_id}?erro=" + quote("Esse negócio ainda está em aberto: feche como Ganho ou Perdido antes de abrir outro."), status_code=302)
    if deals.history(db, lead)[-1].id != lead.id:
        return RedirectResponse(url=f"/leads/{lead_id}?erro=" + quote("Já existe um negócio mais novo pra esse cliente."), status_code=302)
    new = deals.open_new(db, lead, by_customer=False, user_id=user.id)
    return RedirectResponse(url=f"/inbox/{new.id}" if new.conversations else f"/leads/{new.id}?salvo=1", status_code=302)


@router.post("/leads/{lead_id}/comprovante")
def confirm_receipt(
    lead_id: str,
    acao: str = Form(...),
    valor: str = Form(""),
    voltar: str = Form(""),
    ajax: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """💸 Comprovante lido pela IA: confirmar = marca Ganho com o valor; descartar = não era venda."""
    lead = _lead_for_action(db, tenant, user, lead_id)
    if lead.receipt_status != "pending":
        return JSONResponse({"ok": True}) if ajax else RedirectResponse(url=voltar or f"/inbox/{lead_id}", status_code=302)
    if acao == "confirmar":
        won = next((st for st in funnel._stages(db, tenant.id) if st.is_won), None)
        try:
            amount = _parse_brl(valor) or (lead.receipt_amount or 0)
        except ValueError:
            amount = lead.receipt_amount or 0
        if won is None or amount <= 0:
            raise HTTPException(status_code=400, detail="Valor inválido")
        changed = lead.stage_id != won.id
        funnel.set_stage(lead, won)
        lead.deal_value = amount
        lead.receipt_status = "confirmed"
        shown = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        _add_note(db, lead, f"💸 Venda confirmada pelo comprovante (R$ {shown}) por {user.name}")
        db.commit()
        if changed and won.conversion_event_name:
            dispatch_stage_conversion(db, lead, won.conversion_event_name)
    else:
        lead.receipt_status = "dismissed"
        db.commit()
    if ajax:
        return JSONResponse({"ok": True})
    return RedirectResponse(url=voltar if voltar.startswith("/") else f"/inbox/{lead_id}", status_code=302)


@router.post("/leads/{lead_id}/sugestao")
def answer_suggestion(
    lead_id: str,
    acao: str = Form(...),
    tipo: str = Form(""),
    valor: str = Form(""),
    loss_reason: str = Form(""),
    loss_detail: str = Form(""),
    voltar: str = Form(""),
    ajax: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """💡 Sugestão de etapa (services/suggestions.py): aceitar aplica; "Não" não volta a sugerir."""
    lead = _lead_for_action(db, tenant, user, lead_id)
    back = voltar if voltar.startswith("/") else f"/inbox/{lead_id}"

    def done(error: str = ""):
        if ajax:
            return JSONResponse({"ok": not error, "error": error}, status_code=400 if error else 200)
        return RedirectResponse(url=back + (("&" if "?" in back else "?") + "erro=" + quote(error) if error else ""), status_code=302)

    kind = tipo or lead.suggest_kind
    if acao != "aceitar":
        lead.suggest_kind = kind
        suggestions.dismiss(lead)
        db.commit()
        return done()
    stages = funnel._stages(db, tenant.id)
    target = None
    if kind == "qualificado":
        target = funnel.stage_named(stages, "Qualificado", "Qualificados")
    elif kind == "ganho":
        target = next((st for st in stages if st.is_won), None)
        try:
            amount = _parse_brl(valor) or (lead.quoted_value or 0)
        except ValueError:
            amount = 0
        if amount <= 0:
            return done("Preencha o valor da venda.")
        lead.deal_value = amount
    elif kind == "perdido":
        target = next((st for st in stages if st.is_lost), None)
        error = funnel.check_loss(db, lead, target, loss_reason) if target else "Etapa Perdido não encontrada."
        if error:
            return done(error)
        lead.loss_reason, lead.loss_detail = loss_reason, loss_detail.strip()
    elif kind == "outro":
        lead.tag = "outro"
    if target is not None and (lead.stage is None or target.order > lead.stage.order or target.is_won or target.is_lost):
        changed = lead.stage_id != target.id
        funnel.set_stage(lead, target)
        if changed and target.conversion_event_name:
            dispatch_stage_conversion(db, lead, target.conversion_event_name)
    lead.suggest_kind = ""
    _add_note(db, lead, f"💡 {suggestions.KIND_LABEL.get(kind, kind)}: confirmado por {user.name}")
    db.commit()
    return done()


@router.post("/leads/{lead_id}/encerrar")
def settle_lead(
    lead_id: str,
    voltar: str = Form(""),
    ajax: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """"✓ Encerrar atendimento": o cliente só agradeceu/finalizou, não precisa de resposta.
    Sai do "aguardando resposta" e do "Não respondidas" até ele mandar mensagem de novo."""
    lead = _lead_for_action(db, tenant, user, lead_id)
    response_times.settle(lead, user.id)
    if lead.assigned_user_id == user.id:
        inbox_state.mark_seen_by_seller(lead)
    _add_note(db, lead, f"✓ Atendimento encerrado por {user.name} (sem resposta necessária)")
    db.commit()
    if ajax:
        return JSONResponse({"ok": True})
    return RedirectResponse(url=voltar if voltar.startswith("/") else f"/inbox/{lead_id}", status_code=302)


@router.post("/leads/{lead_id}/transferir")
def transfer_lead(
    lead_id: str,
    para: str = Form(...),
    trocar_numero: str = Form(""),
    ajax: str = Form(""),
    voltar: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Passa o lead (com a conversa inteira) pra outro vendedor. O histórico todo continua no
    CRM: quem recebe abre a conversa e vê tudo, desde a 1ª mensagem. Com `trocar_numero`, as
    próximas mensagens saem pelo WhatsApp do novo vendedor."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin passa conversas pra outro vendedor")
    lead = _lead_for_action(db, tenant, user, lead_id)
    target = next((p for p in team_members(db, tenant.id, only_active=True) if p.id == para), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Vendedor não encontrado")
    previous = lead.assigned_user.name if lead.assigned_user else "ninguém"
    lead.assigned_user_id = target.id
    lead.seen_at = None  # o novo vendedor ainda não viu
    for conversation in lead.conversations:
        conversation.assigned_user_id = target.id
    note = f"↪ Transferido de {previous} para {target.name} por {user.name}"
    if trocar_numero:
        number = (
            db.query(WhatsAppNumber)
            .filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.owner_user_id == target.id, WhatsAppNumber.is_active.is_(True))
            .first()
        )
        if number is not None:
            previous_conv = lead.active_conversation
            conversation = next((c for c in lead.conversations if c.whatsapp_number_id == number.id), None)
            if conversation is None:
                conversation = Conversation(
                    tenant_id=tenant.id, lead_id=lead.id, whatsapp_number_id=number.id, assigned_user_id=target.id,
                    last_preview=previous_conv.last_preview if previous_conv else "",  # a lista mostra a última mensagem de verdade
                )
                db.add(conversation)
            conversation.last_message_at = datetime.datetime.utcnow()  # vira a conversa ativa
            db.flush()
            db.refresh(lead)
            note += f" — próximas mensagens pelo WhatsApp {number.label}"
    _add_note(db, lead, note)
    db.commit()
    inbox_state.mark_unread(db, target.id, lead.id)  # chega destacado no Inbox de quem recebeu
    if ajax:
        return JSONResponse({"ok": True})
    if voltar.startswith("/"):
        return RedirectResponse(url=voltar, status_code=302)
    # vendedor que passou o lead adiante não enxerga mais a conversa
    return RedirectResponse(url=f"/inbox/{lead_id}" if user.role == "admin" else "/inbox?transferido=1", status_code=302)


@router.post("/inbox/{lead_id}/nao-lida")
def inbox_mark_unread(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if _visible_lead(db, tenant, user, lead_id):
        inbox_state.mark_unread(db, user.id, lead_id)
    return JSONResponse({"ok": True})


@router.post("/leads/{lead_id}/tag")
def set_lead_tag(
    lead_id: str,
    tag: str = Form(""),
    voltar: str = Form(""),
    ajax: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Etiqueta do contato: Lead / Cliente / Outro (qualquer pessoa da equipe pode mudar)."""
    lead = _visible_lead(db, tenant, user, lead_id)
    if not lead or tag not in inbox_state.TAG_LABEL:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    lead.tag = tag
    db.add(lead)
    db.commit()
    if ajax:
        return JSONResponse({"ok": True})
    return RedirectResponse(url=voltar if voltar.startswith("/") else f"/inbox/{lead_id}", status_code=302)


@router.get("/inbox-atualizar")
def inbox_refresh(
    lead: str = "",
    busca: str = "",
    numero: str = "",
    vendedor: str = "",
    tipo: str = "",
    lidas: str = "",
    qtd: str = "",
    ordem: str = "recentes",
    etapa: str = "",
    ls: str = "",
    ms: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Chamado a cada poucos segundos pela tela do Inbox: devolve a lista e a
    conversa aberta já desenhadas, com uma "assinatura" pra tela só trocar o
    que mudou (mensagem nova do cliente ou enviada pelo celular)."""
    # checagem rápida (2 consultas leves): só monta a tela se algo mudou
    if user.role != "admin":
        numero = vendedor = ""
    limit = _list_limit(qtd)
    query, _ = _inbox_query(db, tenant, user, busca, numero, vendedor, tipo, lidas, etapa)
    rows = _first_per_lead(
        query.with_entities(Conversation.id, Conversation.lead_id, Conversation.last_message_at)
        .order_by(*_inbox_order(ordem))
        .limit(limit * 2),
        limit,
    )
    current_msg_sig = _msg_sig(db, lead or None)
    if _list_sig(rows) == ls and current_msg_sig == ms:
        return JSONResponse({"changed": False})  # nada novo: resposta mínima, sem desenhar nada
    if lead and current_msg_sig != ms:
        inbox_state.mark_read(db, user.id, lead)  # chegou mensagem na conversa que está aberta: já foi vista
    ctx = _inbox_context(db, tenant, user, lead or None, busca, numero, vendedor, tipo, lidas, limit, ordem, etapa)
    return JSONResponse(
        {
            "list_sig": ctx["list_sig"],
            "list_html": templates.get_template("_inbox_list.html").render(ctx),
            "unread_total": ctx["unread_total"],
            "status_counts": ctx["status_counts"],
            "msg_sig": ctx["msg_sig"],
            "msg_html": templates.get_template("_inbox_messages.html").render(ctx) if lead else "",
        }
    )


# ---------- Visão da equipe: até 4 vendedores lado a lado (só admin) ----------
TEAM_VIEW_MAX = 50  # na prática: a equipe toda (a tela rola de lado)
TEAM_VIEW_PER_COLUMN = 30
TEAM_VIEW_COOKIE = "inbox_equipe"


# filtros de cada coluna (clique nos contadores do cabeçalho), do ponto de vista do VENDEDOR:
#   pend      = ⏳ aguardando resposta (todas)
#   naovistas = 🙈 aguardando e ele nem abriu a conversa (no CRM ou no celular)
#   vistas    = 👀 aguardando, ele abriu/leu e não respondeu
#   grupos    = 👥 só os grupos (sem filtro, a coluna mostra só leads)
#   gruposnv  = 👥 grupos com mensagem nova que ele não viu
TEAM_MODES = {
    "pend": inbox_state.unanswered_filter,
    "naovistas": inbox_state.unseen_filter,
    "vistas": inbox_state.seen_unanswered_filter,
    "grupos": inbox_state.groups_filter,
    "gruposnv": inbox_state.unseen_groups_filter,
    "followup": None,  # 📞 follow-up pendente (precisa do cliente/etapas: tratado em _team_columns)
    "parados": None,  # ⏰ parado na etapa além do prazo
}


def _pending_only(query, mode: str = "pend"):
    """Mesma regra dos contadores do cabeçalho da coluna."""
    return TEAM_MODES.get(mode, inbox_state.unanswered_filter)(query).filter(Lead.archived_at.is_(None), Lead.tag != "outro")


def _team_columns(
    db: Session, tenant: Tenant, user: User, seller_ids: list, modes: Optional[dict] = None, busca: str = "",
    limits: Optional[dict] = None, ordem: str = "recentes",
) -> list:
    """Colunas da visão da equipe. `modes` = filtro de cada coluna (ver TEAM_MODES); `busca`
    filtra todas as listas; `limits` = quantas conversas cada coluna mostra ("Ver mais")."""
    people = {p.id: p for p in team_members(db, tenant.id)}
    limits, modes = limits or {}, modes or {}
    now = datetime.datetime.utcnow()
    today = local_to_utc(to_local(now).replace(hour=0, minute=0, second=0, microsecond=0))
    stages = funnel._stages(db, tenant.id)
    fu_cond, stale_cond = followup.followup_condition(tenant, stages), followup.stale_condition(stages)
    columns = []
    for seller_id in [s for s in seller_ids if s in people][:TEAM_VIEW_MAX]:
        query, _ = _inbox_query(db, tenant, user, "", "", seller_id)
        list_query, _ = _inbox_query(db, tenant, user, busca, "", seller_id)
        mode = modes.get(seller_id, "")
        if mode == "followup":
            list_query = list_query.filter(fu_cond)
        elif mode == "parados":
            list_query = list_query.filter(stale_cond, Lead.is_group.is_(False))
        elif mode:
            list_query = _pending_only(list_query, mode)
        else:
            list_query = list_query.filter(Lead.is_group.is_(False))  # padrão: só leads; grupos no filtro 👥
        limit = limits.get(seller_id, TEAM_VIEW_PER_COLUMN)
        convs = _first_per_lead(
            list_query.options(joinedload(Conversation.lead))
            .order_by(*_inbox_order(ordem))
            .limit(limit * 2),
            limit,
        )
        open_leads = db.query(Lead).filter(
            Lead.tenant_id == tenant.id, Lead.assigned_user_id == seller_id, Lead.deleted_at.is_(None),
            Lead.archived_at.is_(None), Lead.tag != "outro", Lead.is_group.is_(False),
        )
        waiting = open_leads.filter(response_times.awaiting_reply_sql()).count()
        unseen = inbox_state.unseen_filter(open_leads).count() if waiting else 0
        fu_count = open_leads.filter(fu_cond).count()
        stale_count = open_leads.filter(stale_cond).count()
        groups = db.query(Lead).filter(
            Lead.tenant_id == tenant.id, Lead.assigned_user_id == seller_id, Lead.deleted_at.is_(None), Lead.is_group.is_(True)
        )
        group_total = groups.count()
        group_unseen = inbox_state.unseen_groups_filter(groups).count() if group_total else 0
        today_count = query.filter(Conversation.last_message_at >= today).count()
        columns.append({
            "seller": people[seller_id], "convs": convs, "waiting": waiting, "unseen": unseen,
            "seen_waiting": waiting - unseen, "today": today_count, "mode": mode,
            "groups": group_total, "groups_unseen": group_unseen, "followups": fu_count, "stale": stale_count,
            "truncated": len(convs) >= limit and limit < INBOX_LIST_MAX, "limit": limit, "busca": busca,
        })
    # não lidas (de quem está olhando) de todas as colunas numa consulta só
    all_ids = [c.lead_id for col in columns for c in col["convs"]]
    unread, favorites = inbox_state.unread_map(db, user, all_ids), inbox_state.favorite_ids(db, user, all_ids)
    for col in columns:
        col["unread"], col["favorites"] = unread, favorites
    return columns


def _team_sig(db: Session, tenant: Tenant, user: User, seller_ids: list) -> str:
    """Uma consulta só pra todas as colunas (dá pra ter a equipe inteira aberta sem pesar)."""
    seller_ids = seller_ids[:TEAM_VIEW_MAX]
    if not seller_ids:
        return ""
    query, _ = _inbox_query(db, tenant, user, "", "", "")
    rows = dict(
        (row[0], row[1:])
        for row in query.filter(Lead.assigned_user_id.in_(seller_ids))
        .with_entities(
            Lead.assigned_user_id,
            func.count(Conversation.id),
            # mensagem nova, vendedor leu no celular, atendimento encerrado: qualquer um muda a assinatura
            func.max(Conversation.last_message_at),
            func.max(Lead.seen_at),
            func.max(Lead.settled_at),
        )
        .group_by(Lead.assigned_user_id)
    )
    def stamp(values) -> str:
        return ":".join(v.isoformat() if hasattr(v, "isoformat") else str(v or "") for v in values)
    return "|".join(f"{s}:{stamp(rows.get(s, (0,)))}" for s in seller_ids)


TEAM_PENDING_COOKIE = "inbox_equipe_pend"


def _team_pending(request: Request) -> dict:
    """Filtro de cada coluna: ?p=<vendedor>:<modo> (modo em TEAM_MODES; sem modo = "pend").
    Lembrado em cookie."""
    if "p" in request.query_params or "pset" in request.query_params:
        raw = request.query_params.getlist("p")
    else:
        raw = request.cookies.get(TEAM_PENDING_COOKIE, "").split(",")
    modes = {}
    for item in raw:
        seller, _, mode = item.partition(":")
        if seller:
            modes[seller] = mode if mode in TEAM_MODES else "pend"
    return modes


def _modes_cookie(modes: dict) -> str:
    return ",".join(f"{seller}:{mode}" for seller, mode in modes.items())


def _team_filters(request: Request) -> tuple:
    """Lupa (q) e quantas conversas cada coluna mostra (m=<vendedor>:<qtd>)."""
    qp = request.query_params
    limits = {}
    for item in qp.getlist("m"):
        seller, _, qty = item.partition(":")
        if seller:
            limits[seller] = _list_limit(qty, TEAM_VIEW_PER_COLUMN)
    return qp.get("q", "").strip()[:80], limits


def _team_selection(request: Request) -> list:
    chosen = request.query_params.getlist("v") if "v" in request.query_params else (
        [v for v in request.cookies.get(TEAM_VIEW_COOKIE, "").split(",") if v]
    )
    return list(dict.fromkeys(chosen))[:TEAM_VIEW_MAX]


@router.get("/inbox-equipe", response_class=HTMLResponse)
def team_inbox(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Admin escolhe até 4 vendedores e vê as conversas de cada um lado a lado."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin vê a visão da equipe")
    selected = _team_selection(request)
    pending = _team_pending(request)
    busca, limits = _team_filters(request)
    ordem = _order_choice(request, "equipe_ordem")
    columns = _team_columns(db, tenant, user, selected, pending, busca, limits, ordem)
    response = templates.TemplateResponse(
        request,
        "inbox_equipe.html",
        {
            "tenant": tenant,
            "user": user,
            "active_nav": "inbox",
            "people": team_members(db, tenant.id),
            "selected": selected,
            "columns": columns,
            "sig": _team_sig(db, tenant, user, selected),
            "max": TEAM_VIEW_MAX,
            "busca": busca,
            "per_column": TEAM_VIEW_PER_COLUMN,
            "ordem": ordem,
            "order_labels": ORDER_LABELS,
            "quick_replies": replies_for_js(db, tenant, user),
        },
    )
    response.set_cookie(TEAM_VIEW_COOKIE, ",".join(selected), httponly=True, samesite="lax")
    response.set_cookie(TEAM_PENDING_COOKIE, _modes_cookie(pending), httponly=True, samesite="lax")
    response.set_cookie("equipe_ordem", ordem, httponly=True, samesite="lax")
    return response


TEAM_THREAD_LAST = 80  # mensagens mostradas na coluna (a conversa inteira abre no Inbox)


@router.get("/inbox-equipe/conversa/{lead_id}")
def team_thread(
    lead_id: str,
    ms: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Conversa aberta dentro de uma coluna da visão da equipe. Com `ms` (assinatura
    que a coluna já tem) só desenha de novo se chegou/saiu mensagem. Também usada na
    tela "leads da campanha" (aí o vendedor pode abrir as conversas dos leads dele)."""
    if user.role != "admin" and not db.query(Lead.id).filter(
        Lead.id == lead_id, Lead.tenant_id == tenant.id, Lead.assigned_user_id == user.id
    ).first():
        raise HTTPException(status_code=403)
    current = _msg_sig(db, lead_id)
    if ms and current == ms:
        return JSONResponse({"changed": False})
    lead = _visible_lead(db, tenant, user, lead_id)
    if not lead:
        raise HTTPException(status_code=404)
    inbox_state.mark_read(db, user.id, lead_id)
    messages = lead.all_messages[-TEAM_THREAD_LAST:]
    closed = bool(lead.stage and (lead.stage.is_won or lead.stage.is_lost))
    head = templates.get_template("_team_thread_head.html").render({
        "lead": lead, "closed": closed, "truncated": len(lead.all_messages) > TEAM_THREAD_LAST,
        "can_settle": not lead.is_group and bool(response_times.waiting_since(lead)),
        "transfer_people": [] if lead.is_group or user.role != "admin" else _transfer_options(db, tenant, lead),
        "stages": [] if lead.is_group else db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all(),
    })
    body = templates.get_template("_inbox_messages.html").render({"messages": messages})
    return JSONResponse({"sig": current, "head": head, "html": body})


@router.get("/inbox-equipe/atualizar")
def team_inbox_refresh(
    request: Request,
    sig: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        raise HTTPException(status_code=403)
    selected = _team_selection(request)
    pending = _team_pending(request)
    current = _team_sig(db, tenant, user, selected)
    if current == sig:
        return JSONResponse({"changed": False})
    busca, limits = _team_filters(request)
    ordem = _order_choice(request, "equipe_ordem")
    html = templates.get_template("_inbox_equipe_cols.html").render(
        {"columns": _team_columns(db, tenant, user, selected, pending, busca, limits, ordem), "request": request}
    )
    response = JSONResponse({"sig": current, "html": html})
    response.set_cookie(TEAM_PENDING_COOKIE, _modes_cookie(pending), httponly=True, samesite="lax")
    return response


AVATAR_TTL = 24 * 60 * 60
AVATAR_DIR = os.getenv("AVATAR_CACHE_DIR", "/tmp/crm-avatars")
_AVATAR_SLOTS = threading.BoundedSemaphore(4)  # no máximo 4 buscas no WhatsApp ao mesmo tempo
# PNG 1x1 transparente: "sem foto" (a letra inicial continua aparecendo por baixo)
_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _blank_avatar(max_age: int) -> Response:
    return Response(content=_BLANK_PNG, media_type="image/png", headers={"Cache-Control": f"private, max-age={max_age}"})


def _fresh(path: str) -> bool:
    return os.path.exists(path) and time.time() - os.path.getmtime(path) < AVATAR_TTL


@router.get("/leads/{lead_id}/avatar")
def lead_avatar(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Foto de perfil do WhatsApp do lead, guardada em disco por 24h. Sem foto (ou
    WhatsApp ocupado) devolve uma imagem transparente: a tela mostra a inicial."""
    os.makedirs(AVATAR_DIR, exist_ok=True)
    photo, none_marker = os.path.join(AVATAR_DIR, lead_id), os.path.join(AVATAR_DIR, f"{lead_id}.none")
    if _fresh(photo):
        with open(photo, "rb") as fh:
            return Response(content=fh.read(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    if _fresh(none_marker):
        return _blank_avatar(86400)

    lead = _visible_lead(db, tenant, user, lead_id)
    conversation = lead.active_conversation if lead else None
    number = conversation.whatsapp_number if conversation else None
    if not lead or not number or number.provider != "evolution":
        return _blank_avatar(86400)
    instance, phone = number.evolution_instance, lead.phone
    db.close()  # devolve a conexão do banco antes de ir no WhatsApp

    if not _AVATAR_SLOTS.acquire(blocking=False):
        return _blank_avatar(60)  # muita busca ao mesmo tempo: tenta de novo daqui a pouco
    try:
        content = b""
        try:
            url = evolution_client.profile_picture_url(instance, phone)
            if url:
                resp = requests.get(url, timeout=6)
                content = resp.content if resp.ok else b""
        except (evolution_client.EvolutionError, requests.RequestException):
            return _blank_avatar(300)
        if not content:
            open(none_marker, "wb").close()
            return _blank_avatar(86400)
        with open(photo, "wb") as fh:
            fh.write(content)
        return Response(content=content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    finally:
        _AVATAR_SLOTS.release()


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
    if not conversation or conversation.tenant_id != tenant.id or not _visible_lead(db, tenant, user, conversation.lead_id):
        raise HTTPException(status_code=404, detail="Mídia não encontrada")

    # 1º a cópia própria (sobrevive a número bloqueado); senão busca no WhatsApp
    content, mime_type = media_store.load(message.media_stored_key)
    if content is None:
        number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
        content, mime_type = messaging.fetch_media(number, message.media_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Mídia indisponível (expirou ou o número foi desconectado)")

    # a mídia de uma mensagem nunca muda: o navegador guarda e não baixa de novo a cada
    # atualização da conversa (antes cada mensagem nova recarregava todas as fotos)
    return Response(content=content, media_type=mime_type, headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.post("/leads/{lead_id}/reply")
def reply_lead(
    lead_id: str,
    body: str = Form(...),
    ajax: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = _visible_lead(db, tenant, user, lead_id)
    if not lead or not lead.conversations:
        return RedirectResponse(url=f"/inbox/{lead_id}", status_code=302)

    conversation = lead.active_conversation
    number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
    ok, wa_id, secret = messaging.send_text(number, lead.phone, body)
    if ok:
        response_times.mark_outbound(lead)
    _record_outbound(db, conversation, user, wa_id, body if ok else f"⚠️ Não enviada: {body}", secret=secret)
    if ajax:
        return JSONResponse({"ok": ok, "error": "" if ok else "o WhatsApp deste número não aceitou o envio"})
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
    saem pelo número escolhido. Só admin."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin troca o WhatsApp de envio")
    lead = _visible_lead(db, tenant, user, lead_id)
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
def reply_lead_media(
    lead_id: str,
    file: UploadFile,
    background: BackgroundTasks,
    caption: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    """Envia foto/vídeo/documento pro lead. Chamado via fetch() do Inbox (não
    é um form comum) pra dar pra mostrar 'enviando...' sem recarregar a
    página até a resposta da Meta confirmar."""
    lead = _visible_lead(db, tenant, user, lead_id)
    if not lead or not lead.conversations:
        return JSONResponse({"ok": False, "error": "conversa não encontrada"}, status_code=404)

    content = file.file.read()
    max_bytes = 16 * 1024 * 1024  # limite da própria Cloud API pra imagem/doc (vídeo é maior, mas fica um teto seguro)
    if len(content) > max_bytes:
        return JSONResponse({"ok": False, "error": "arquivo maior que 16MB"}, status_code=400)

    conversation = lead.active_conversation
    number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
    mime_type = file.content_type or "application/octet-stream"
    media_kind = media_kind_for_mime(mime_type)

    ok, wa_id, media_id, error, secret = messaging.send_media(
        number, lead.phone, content, file.filename or "arquivo", mime_type, media_kind, caption
    )
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=502)
    response_times.mark_outbound(lead)

    placeholder = {"image": "📷 Imagem", "video": "🎥 Vídeo", "audio": "🎤 Áudio", "document": "📄 Documento"}[media_kind]
    message = _record_outbound(
        db,
        conversation,
        user,
        wa_id,
        f"{placeholder}{' — ' + caption if caption else ''}",
        media_id,
        media_kind,
        secret=secret,
    )
    background.add_task(media_store.store_sent_copy, message.id, content, mime_type)
    return JSONResponse({"ok": True})


def _record_outbound(
    db: Session,
    conversation: Conversation,
    user: User,
    wa_id: str,
    body: str,
    media_id: str = "",
    media_type: str = "",
    secret: str = "",
) -> Message:
    # no Evolution o webhook "fromMe" dessa mesma mensagem pode chegar antes
    # deste commit; aí ela já está salva e só marcamos quem enviou
    lead = db.get(Lead, conversation.lead_id)
    if lead is not None and wa_id:  # enviada de verdade (não conta "⚠️ Não enviada")
        funnel.on_message(db, lead, outbound=True)  # time falou: vai pra "Em atendimento"
        followup.on_outbound_text(db, lead, body)  # mandou preço: vai pra "Negociando"
        suggestions.on_message(lead, body, outbound=True)  # 💡 "temos sim", "não temos", "pedido confirmado"...
    existing = db.query(Message).filter(Message.wa_message_id == wa_id).first() if wa_id else None
    message = existing or Message(conversation_id=conversation.id, direction="out", wa_message_id=wa_id)
    message.sender_user_id = user.id
    message.body = body
    message.media_id = media_id or message.media_id
    message.media_type = media_type or message.media_type
    message.secret = secret or message.secret
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
    date_range: str = "30d",
    date_from: str = "",
    date_to: str = "",
    seller: str = "",
    n: int = 20,
    aba: str = "comercial",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    sellers = team_members(db, tenant.id)
    stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
    is_admin = user.role == "admin"
    if not is_admin:
        seller = ""  # vendedor vê os números gerais da equipe + os dele (nunca os de outro vendedor)
    # abas: Comercial (padrão) · Tráfego · Clientes. Vendedor só vê a Comercial.
    if aba not in ("comercial", "trafego", "clientes") or not is_admin:
        aba = "comercial"

    now = datetime.datetime.utcnow()
    range_to: Optional[datetime.datetime] = None
    if date_range == "custom":
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
        range_from = local_to_utc(datetime.datetime.combine(parsed_from, datetime.time.min)) if parsed_from else None
        range_to = local_to_utc(datetime.datetime.combine(parsed_to, datetime.time.max)) if parsed_to else None
    else:
        range_from = _range_from(now, date_range)

    leads_query = db.query(Lead).filter(
        Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None), Lead.tag != "outro", Lead.is_group.is_(False)
    )
    if range_from:
        leads_query = leads_query.filter(Lead.created_at >= range_from)
    if range_to:
        leads_query = leads_query.filter(Lead.created_at <= range_to)
    all_period_leads = leads_query.options(*LEAD_CARD_LOAD).all()
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

    # tempo de atendimento (período + vendedor escolhidos); "aguardando" e "sem interação" só em aberto
    open_leads = [lead for lead in leads if lead.stage_id not in won_stage_ids and lead.stage_id not in lost_stage_ids]
    rt_all, rt_open = response_times.summary(leads), response_times.summary(open_leads)
    rt_box = {
        "median_first": rt_all["median_first"],
        "within_15": rt_all["within_15"],
        "answered": rt_all["answered"],
        "waiting": rt_open["never_answered"],
        "idle": rt_open["idle"],
    }

    def seller_row(person, mine: list) -> dict:
        won = [lead for lead in mine if lead.stage_id in won_stage_ids]
        mine_open = [l for l in mine if l.stage_id not in won_stage_ids and l.stage_id not in lost_stage_ids]
        mine_rt = response_times.summary(mine)
        return {
            "user": person,
            "total": len(mine),
            "by_stage": {st.id: sum(1 for lead in mine if lead.stage_id == st.id) for st in stages},
            "won": len(won),
            "conversion": round(len(won) / len(mine) * 100) if mine else 0,
            "revenue": sum(lead.deal_value or 0 for lead in won),
            "median_first": mine_rt["median_first"],
            "within_15": mine_rt["within_15"],
            "waiting": response_times.summary(mine_open)["never_answered"],
            "idle": response_times.summary(mine_open)["idle"],
            "followups": sum(1 for l in mine_open if followup.state(l)["followup"]),
            "stale": sum(1 for l in mine_open if (followup.state(l)["stale"] or {}).get("late")),
        }

    # funil lado a lado por vendedor: só vendedores de fato (admin não entra); o vendedor
    # logado vê só a linha dele + a linha da equipe toda
    seller_rows = []
    for s in sellers:
        if s.role != "agent" or (not is_admin and s.id != user.id):
            continue
        mine = [lead for lead in all_period_leads if lead.assigned_user_id == s.id]
        if not mine and not s.is_active:
            continue
        seller_rows.append(seller_row(s, mine))
    seller_rows.sort(key=lambda r: (r["won"], r["total"]), reverse=True)
    team_row = seller_row(None, all_period_leads)
    commercial_funnel = funnel.summary(leads, stages)
    lost_leads = [lead for lead in leads if lead.stage_id in lost_stage_ids]
    loss_counts: dict = {}
    for lead in lost_leads:
        key = lead.loss_reason or "Sem motivo informado"
        loss_counts[key] = loss_counts.get(key, 0) + 1
    loss_rows = [
        {"reason": r, "count": n, "pct": round(100 * n / len(lost_leads), 1)}
        for r, n in sorted(loss_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    my_numbers = None
    if not is_admin:
        mine = [lead for lead in all_period_leads if lead.assigned_user_id == user.id]
        my_numbers = {"row": seller_row(user, mine), "funnel": funnel.summary(mine, stages)}
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
    table_leads = leads if is_admin else [lead for lead in leads if lead.assigned_user_id == user.id]
    if card == "new_24h":
        table_leads = [lead for lead in table_leads if lead.created_at >= last_24h]
    elif card == "no_contact":
        table_leads = [lead for lead in table_leads if is_no_contact_24h(lead)]
    elif card == "won":
        table_leads = [lead for lead in table_leads if lead.stage_id in won_stage_ids]
    elif card == "receita":
        table_leads = [lead for lead in table_leads if lead.stage_id in won_stage_ids and lead.deal_value]

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

    if card == "receita":  # de onde veio a receita: maiores vendas primeiro
        table_leads = sorted(table_leads, key=lambda lead: lead.deal_value or 0, reverse=True)
    else:
        table_leads = sorted(table_leads, key=lambda lead: lead.created_at, reverse=True)
    # todos os leads do filtro contam no título; a lista mostra 20 e o "Ver mais" traz +50
    table_total, table_value = len(table_leads), sum(lead.deal_value or 0 for lead in table_leads)
    shown = max(20, min(n, 5000))
    table_leads = table_leads[:shown]
    more_url = "/dashboard?" + urlencode({**dict(request.query_params), "n": shown + 50}) + "#leads"
    stage_by_id = {s.id: s for s in stages}

    # ---- tráfego: gasto dos anúncios x leads e vendas, por campanha > conjunto > anúncio ----
    traffic_data = traffic.build(
        db, tenant.id, leads,
        to_local(range_from).date() if range_from else None, to_local(range_to).date() if range_to else None,
        funnel.is_qualified(stages), set(won_stage_ids),
    ) if aba == "trafego" else None  # cada aba calcula só o que mostra

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
            "table_total": table_total,
            "table_value": table_value,
            "more_url": more_url if table_total > shown else "",
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
            "rt_box": rt_box,
            "unassigned": unassigned,
            "traffic": traffic_data,
            "is_admin": is_admin,
            "team_row": team_row,
            "commercial_funnel": commercial_funnel,
            "loss_rows": loss_rows,
            "lost_total": len(lost_leads),
            "my_stats": my_numbers,
            "customer_stats": deals.customer_stats(
                db, tenant.id, won_stage_ids, range_from, range_to, seller=seller, top_owner="" if is_admin else user.id
            ) if aba == "clientes" else None,
            "aba": aba,
            "meta_accounts": tenant.meta_ad_accounts,
        },
    )
