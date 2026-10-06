import base64
import datetime
import os
import threading
import time
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import CampaignSpend, Conversation, Lead, Message, PipelineStage, Tenant, User, WhatsAppNumber
from app.services import evolution_client, inbox_state, media_store, messaging, response_times
from app.services.conversions.dispatcher import dispatch_stage_conversion
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

    base = db.query(Lead).filter(
        Lead.tenant_id == tenant.id, Lead.archived_at.is_(None), Lead.deleted_at.is_(None), Lead.tag != "outro"
    )
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
            base.filter(Lead.stage_id == stage.id)
            .options(*LEAD_CARD_LOAD)  # carrega origem/conversas/atendente junto (sem 1 consulta por cartão)
            .order_by(Lead.updated_at.desc())
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
            "sellers": team_members(db, tenant.id),
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


def _inbox_query(
    db: Session, tenant: Tenant, user: User, busca: str, numero: str, vendedor: str, tipo: str = "", lidas: str = ""
):
    """Conversas visíveis no Inbox com os filtros aplicados. Devolve (query, removed_ids)."""
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
    if lidas == "nao":
        query = inbox_state.unread_filter(query, user)
    if tipo == "leads":
        query = query.filter(Lead.tag == "")
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


def _first_per_lead(rows) -> list:
    """Um item por lead (um lead pode ter conversa em mais de um número), o mais recente primeiro."""
    picked, seen = [], set()
    for row in rows:
        if row.lead_id not in seen and len(picked) < INBOX_LIST_LIMIT:
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
) -> dict:
    if user.role != "admin":
        numero = vendedor = ""
    query, removed_ids = _inbox_query(db, tenant, user, busca, numero, vendedor, tipo, lidas)
    # quantas não lidas existem com os filtros atuais (pro botão "Não lidas (N)")
    unread_query, _ = _inbox_query(db, tenant, user, busca, numero, vendedor, tipo, "nao")
    unread_total = unread_query.with_entities(func.count(func.distinct(Conversation.lead_id))).scalar() or 0
    conversations = _first_per_lead(
        query.options(joinedload(Conversation.lead).joinedload(Lead.assigned_user))  # sem 1 consulta por item
        .order_by(Conversation.last_message_at.desc())
        .limit(INBOX_LIST_LIMIT * 2)
    )

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
            "list_sig": _list_sig(conversations),
            "unread": inbox_state.unread_map(db, user, [c.lead_id for c in conversations]),
            "tipo": tipo,
            "lidas": lidas,
            "unread_total": unread_total,
            "tags": inbox_state.TAGS,
            "tag_label": inbox_state.TAG_LABEL,
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
            "filter_sellers": team_members(db, tenant.id) if user.role == "admin" else [],
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
            "platform_label": PLATFORM_LABEL[resolve_platform(selected_lead.attribution)] if selected_lead else None,
    }


INBOX_FILTER_COOKIE = "inbox_filtro"


def _render_inbox(request: Request, db: Session, tenant: Tenant, user: User, selected_lead_id: Optional[str]):
    # filtros de número/vendedor ficam lembrados (cookie) ao abrir conversa, responder, etc.
    qp = request.query_params
    saved = (request.cookies.get(INBOX_FILTER_COOKIE, "") + "|||").split("|")
    numero = qp.get("numero", saved[0]) if "numero" in qp or "vendedor" in qp else saved[0]
    vendedor = qp.get("vendedor", saved[1]) if "numero" in qp or "vendedor" in qp else saved[1]
    tipo = qp.get("tipo", saved[2])
    lidas = qp.get("lidas", saved[3])
    inbox_state.ensure_baseline(db, user)
    if selected_lead_id:
        inbox_state.mark_read(db, user.id, selected_lead_id)
    ctx = _inbox_context(db, tenant, user, selected_lead_id, qp.get("busca", ""), numero, vendedor, tipo, lidas)
    response = templates.TemplateResponse(request, "inbox.html", ctx)
    response.set_cookie(INBOX_FILTER_COOKIE, f"{numero}|{vendedor}|{tipo}|{lidas}", httponly=True, samesite="lax")
    return response


@router.post("/inbox/{lead_id}/lida")
def inbox_mark_read(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if db.query(Lead.id).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first():
        inbox_state.mark_read(db, user.id, lead_id)
    return JSONResponse({"ok": True})


@router.post("/inbox/{lead_id}/nao-lida")
def inbox_mark_unread(
    lead_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if db.query(Lead.id).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first():
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
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
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
    query, _ = _inbox_query(db, tenant, user, busca, numero, vendedor, tipo, lidas)
    rows = _first_per_lead(
        query.with_entities(Conversation.id, Conversation.lead_id, Conversation.last_message_at)
        .order_by(Conversation.last_message_at.desc())
        .limit(INBOX_LIST_LIMIT * 2)
    )
    current_msg_sig = _msg_sig(db, lead or None)
    if _list_sig(rows) == ls and current_msg_sig == ms:
        return JSONResponse({"changed": False})  # nada novo: resposta mínima, sem desenhar nada
    if lead and current_msg_sig != ms:
        inbox_state.mark_read(db, user.id, lead)  # chegou mensagem na conversa que está aberta: já foi vista
    ctx = _inbox_context(db, tenant, user, lead or None, busca, numero, vendedor, tipo, lidas)
    return JSONResponse(
        {
            "list_sig": ctx["list_sig"],
            "list_html": templates.get_template("_inbox_list.html").render(ctx),
            "unread_total": ctx["unread_total"],
            "msg_sig": ctx["msg_sig"],
            "msg_html": templates.get_template("_inbox_messages.html").render(ctx) if lead else "",
        }
    )


# ---------- Visão da equipe: até 4 vendedores lado a lado (só admin) ----------
TEAM_VIEW_MAX = 4
TEAM_VIEW_PER_COLUMN = 30
TEAM_VIEW_COOKIE = "inbox_equipe"


def _team_columns(db: Session, tenant: Tenant, user: User, seller_ids: list) -> list:
    people = {p.id: p for p in team_members(db, tenant.id)}
    now = datetime.datetime.utcnow()
    today = local_to_utc(to_local(now).replace(hour=0, minute=0, second=0, microsecond=0))
    columns = []
    for seller_id in [s for s in seller_ids if s in people][:TEAM_VIEW_MAX]:
        query, _ = _inbox_query(db, tenant, user, "", "", seller_id)
        convs = _first_per_lead(
            query.options(joinedload(Conversation.lead))
            .order_by(Conversation.last_message_at.desc())
            .limit(TEAM_VIEW_PER_COLUMN * 2)
        )
        open_leads = db.query(Lead).filter(
            Lead.tenant_id == tenant.id, Lead.assigned_user_id == seller_id, Lead.deleted_at.is_(None),
            Lead.archived_at.is_(None), Lead.tag != "outro",
        )
        waiting = open_leads.filter(
            or_(
                Lead.first_response_at.is_(None),
                and_(Lead.last_inbound_at.isnot(None), or_(Lead.last_outbound_at.is_(None), Lead.last_inbound_at > Lead.last_outbound_at)),
            ),
            Lead.last_inbound_at.isnot(None),
        ).count()
        today_count = query.filter(Conversation.last_message_at >= today).count()
        columns.append({"seller": people[seller_id], "convs": convs[:TEAM_VIEW_PER_COLUMN], "waiting": waiting, "today": today_count})
    # não lidas (de quem está olhando) de todas as colunas numa consulta só
    unread = inbox_state.unread_map(db, user, [c.lead_id for col in columns for c in col["convs"]])
    for col in columns:
        col["unread"] = unread
    return columns


def _team_sig(db: Session, tenant: Tenant, user: User, seller_ids: list) -> str:
    parts = []
    for seller_id in seller_ids[:TEAM_VIEW_MAX]:
        query, _ = _inbox_query(db, tenant, user, "", "", seller_id)
        row = query.with_entities(func.count(Conversation.id), func.max(Conversation.last_message_at)).one()
        parts.append(f"{seller_id}:{row[0]}:{row[1].isoformat() if row[1] else ''}")
    return "|".join(parts)


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
    columns = _team_columns(db, tenant, user, selected)
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
        },
    )
    response.set_cookie(TEAM_VIEW_COOKIE, ",".join(selected), httponly=True, samesite="lax")
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
    que a coluna já tem) só desenha de novo se chegou/saiu mensagem."""
    if user.role != "admin":
        raise HTTPException(status_code=403)
    current = _msg_sig(db, lead_id)
    if ms and current == ms:
        return JSONResponse({"changed": False})
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
    if not lead:
        raise HTTPException(status_code=404)
    inbox_state.mark_read(db, user.id, lead_id)
    messages = lead.all_messages[-TEAM_THREAD_LAST:]
    closed = bool(lead.stage and (lead.stage.is_won or lead.stage.is_lost))
    head = templates.get_template("_team_thread_head.html").render({"lead": lead, "closed": closed, "truncated": len(lead.all_messages) > TEAM_THREAD_LAST})
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
    current = _team_sig(db, tenant, user, selected)
    if current == sig:
        return JSONResponse({"changed": False})
    html = templates.get_template("_inbox_equipe_cols.html").render(
        {"columns": _team_columns(db, tenant, user, selected), "request": request}
    )
    return JSONResponse({"sig": current, "html": html})


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

    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
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
    ajax: str = Form(""),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.tenant_id == tenant.id).first()
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
def reply_lead_media(
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
    secret: str = "",
) -> Message:
    # no Evolution o webhook "fromMe" dessa mesma mensagem pode chegar antes
    # deste commit; aí ela já está salva e só marcamos quem enviou
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
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    sellers = team_members(db, tenant.id)
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

    leads_query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None), Lead.tag != "outro")
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

    # funil lado a lado por vendedor (mesmo período; ignora o filtro de vendedor)
    seller_rows = []
    for s in sellers:
        mine = [lead for lead in all_period_leads if lead.assigned_user_id == s.id]
        if not mine and not s.is_active:
            continue
        won = [lead for lead in mine if lead.stage_id in won_stage_ids]
        mine_rt = response_times.summary(mine)
        mine_open = [l for l in mine if l.stage_id not in won_stage_ids and l.stage_id not in lost_stage_ids]
        seller_rows.append(
            {
                "user": s,
                "total": len(mine),
                "by_stage": {st.id: sum(1 for lead in mine if lead.stage_id == st.id) for st in stages},
                "won": len(won),
                "conversion": round(len(won) / len(mine) * 100) if mine else 0,
                "revenue": sum(lead.deal_value or 0 for lead in won),
                "median_first": mine_rt["median_first"],
                "waiting": response_times.summary(mine_open)["never_answered"],
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
            "rt_box": rt_box,
            "unassigned": unassigned,
            "traffic_rows": traffic_rows,
            "has_spend_data": has_spend_data,
        },
    )
