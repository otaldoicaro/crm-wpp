"""🔎 Auditoria (só admin): quando cada lead entrou e cada contato que ele recebeu.

- /auditoria: leads que entraram no período (hoje, 7 ou 30 dias) ou a busca por nome/telefone, com a hora
  de entrada, o 1º contato (e quanto demorou), quantas mensagens recebeu da loja e se respondeu;
- /auditoria?lead=<id>: a linha do tempo do lead — entrada (origem e formulário), trocas de vendedor e
  cada mensagem (nossa, do cliente, automática), com hora, WhatsApp e quem mandou. Inclui as conversas
  de todos os números (admin vê tudo, também dos contatos Time/Outro).

Na lista: clicar no cabeçalho de uma coluna ordena (de novo = inverte), e dá pra filtrar por vendedor,
origem, situação do contato e período (inclusive datas escolhidas). Na linha do tempo: filtro por tipo
de ação e por quem mandou."""

import datetime

from app import features
from fastapi import APIRouter, Depends, HTTPException, Request
from urllib.parse import urlencode
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_tenant, current_user_required
from app.models import Conversation, Lead, Message, Tenant, User
from app.services import inbox_state, ownership
from app.services.people import team_members
from app.services.platform import PLATFORM_LABEL, resolve_platform
from app.templating import templates
from app.timeutil import local_to_utc, to_local

router = APIRouter()

PERIODS = [("hoje", "Hoje"), ("7d", "7 dias"), ("30d", "30 dias"), ("datas", "Escolher datas")]
LIST_LIMIT = 200
FETCH_LIMIT = 2000  # filtros de origem/situação e ordem por colunas calculadas são feitos depois da consulta
SITUATIONS = [("", "Todas"), ("sem_contato", "Sem 1º contato"), ("contatado", "Com 1º contato"),
              ("respondeu", "Cliente respondeu"), ("nao_respondeu", "Cliente não respondeu")]
# coluna → texto do cabeçalho (ordem da tabela)
COLUMNS = [("entrou", "Entrou"), ("lead", "Lead"), ("origem", "Origem"), ("vendedor", "Vendedor · etapa"),
           ("contato", "1º contato"), ("recebeu", "Recebeu"), ("respondeu", "Respondeu")]
EVENT_KINDS = [("entrada", "Entrada"), ("in", "Cliente escreveu"), ("out", "Loja mandou"),
               ("falhou", "Não enviada"), ("auto", "Registro do CRM")]


def _parse_day(value: str):
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _sort_key(column: str, lead: Lead, summary: dict, platform: str):
    """Chave de ordem de cada coluna (vazio sempre vai pro fim na ordem crescente)."""
    far = datetime.datetime.max
    if column == "lead":
        return (lead.name or lead.phone or "").lower()
    if column == "origem":
        return platform.lower()
    if column == "vendedor":
        return ((lead.assigned_user.name if lead.assigned_user else "~").lower(), lead.stage.name.lower() if lead.stage else "")
    if column == "contato":  # quanto demorou o 1º contato (sem contato = fim)
        first = summary.get("first_out")
        return (first - lead.created_at).total_seconds() if first and lead.created_at else float("inf")
    if column == "recebeu":
        return summary.get("outs", 0)
    if column == "respondeu":
        return (1 if summary.get("ins") else 0, summary.get("first_in") or far)
    return lead.created_at or far


def _since(period: str) -> datetime.datetime:
    today = to_local(datetime.datetime.utcnow()).date()
    days = {"hoje": 0, "7d": 6, "30d": 29}.get(period, 0)
    return local_to_utc(datetime.datetime.combine(today - datetime.timedelta(days=days), datetime.time.min))


def _summaries(db: Session, lead_ids: list) -> dict:
    """{lead_id: {"first_out", "outs", "ins", "first_in"}} das mensagens de verdade (sem notas)."""
    if not lead_ids:
        return {}
    rows = (db.query(Conversation.lead_id, Message.direction, func.count(Message.id), func.min(Message.created_at))
            .join(Message, Message.conversation_id == Conversation.id)
            .filter(Conversation.lead_id.in_(lead_ids), Message.direction.in_(("in", "out")))
            .group_by(Conversation.lead_id, Message.direction).all())
    out = {}
    for lead_id, direction, count, first in rows:
        item = out.setdefault(lead_id, {"outs": 0, "ins": 0, "first_out": None, "first_in": None})
        item["outs" if direction == "out" else "ins"] = count
        item["first_out" if direction == "out" else "first_in"] = first
    return out


def _timeline(lead: Lead) -> list:
    """[(quando, tipo, título, texto, quem)] em ordem de hora. tipo: entrada | out | in | auto | note | dono."""
    events = [(lead.created_at, "entrada", "Entrou no CRM", "", "")]
    if not inbox_state.mirrored(lead):  # 🔒 "não espelhar": a conversa não aparece nem aqui
        return events + [(lead.created_at, "auto", "🔒 Conversa não espelhada no CRM", "", "")]
    for conv in lead.conversations:
        number = conv.whatsapp_number.label if conv.whatsapp_number else "WhatsApp removido"
        for msg in conv.messages:
            body = msg.body or (f"[{msg.media_type}]" if msg.media_type else "")
            if msg.direction == "in":
                events.append((msg.created_at, "in", f"Cliente escreveu · {number}", body, "Cliente"))
            elif msg.direction == "out":
                who = msg.sender.name if msg.sender else (msg.sender_name or "🤖 automática / celular")
                kind = "falhou" if body.startswith("⚠️ Não enviada") else "out"
                events.append((msg.created_at, kind, f"Loja mandou · {number} · {who}", body, who))
            elif body.startswith(("🤖", "📅", "↪", "👤", "📵")):
                events.append((msg.created_at, "auto", "Registro do CRM", body, "CRM"))
    events.sort(key=lambda e: e[0] or datetime.datetime.min)
    return events


@router.get("/auditoria", response_class=HTMLResponse)
def audit_page(
    request: Request,
    periodo: str = "hoje",
    q: str = "",
    lead: str = "",
    vendedor: str = "",
    origem: str = "",
    situacao: str = "",
    de: str = "",
    ate: str = "",
    ordem: str = "entrou",
    dir: str = "",
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
    user: User = Depends(current_user_required),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Só admin vê a Auditoria")
    periodo = periodo if periodo in dict(PERIODS) else "hoje"
    q = q.strip()[:80]
    people = team_members(db, tenant.id)
    if vendedor not in ("", "sem") and vendedor not in {p.id for p in people}:
        vendedor = ""
    origem = origem if origem in PLATFORM_LABEL else ""
    situacao = situacao if situacao in dict(SITUATIONS) else ""
    ordem = ordem if ordem in dict(COLUMNS) else "entrou"
    descending = dir != "asc" if dir in ("asc", "desc") else ordem in ("entrou", "recebeu", "respondeu")
    day_from, day_to = _parse_day(de), _parse_day(ate)
    query = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.is_group.is_(False),
                                  Lead.deleted_at.is_(None))  # excluído (Lixeira) não aparece na Auditoria
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        query = query.filter(or_(Lead.name.ilike(f"%{q}%"), *([Lead.phone.like(f"%{digits}%")] if len(digits) >= 4 else [])))
    elif periodo == "datas":
        if day_from:
            query = query.filter(Lead.created_at >= local_to_utc(datetime.datetime.combine(day_from, datetime.time.min)))
        if day_to:
            query = query.filter(Lead.created_at <= local_to_utc(datetime.datetime.combine(day_to, datetime.time.max)))
    else:
        query = query.filter(Lead.created_at >= _since(periodo))
    if vendedor == "sem":
        query = query.filter(Lead.assigned_user_id.is_(None))
    elif vendedor:
        query = query.filter(Lead.assigned_user_id == vendedor)
    leads = (query.options(selectinload(Lead.attribution), selectinload(Lead.assigned_user), selectinload(Lead.stage))
             .order_by(Lead.created_at.desc()).limit(FETCH_LIMIT).all())
    summaries = _summaries(db, [l.id for l in leads])
    platforms = {l.id: resolve_platform(l.attribution) for l in leads}
    if origem:
        leads = [l for l in leads if platforms[l.id] == origem]
    if situacao:
        def matches(l):
            s = summaries.get(l.id, {})
            return {"sem_contato": not s.get("first_out"), "contatado": bool(s.get("first_out")),
                    "respondeu": bool(s.get("ins")), "nao_respondeu": not s.get("ins")}[situacao]
        leads = [l for l in leads if matches(l)]
    total = len(leads)
    leads.sort(key=lambda l: _sort_key(ordem, l, summaries.get(l.id, {}), PLATFORM_LABEL[platforms[l.id]]), reverse=descending)
    leads = leads[:LIST_LIMIT]
    selected = db.query(Lead).filter(Lead.id == lead, Lead.tenant_id == tenant.id).first() if lead else None
    filters = {"periodo": periodo, "q": q, "vendedor": vendedor, "origem": origem, "situacao": situacao,
               "de": de if periodo == "datas" else "", "ate": ate if periodo == "datas" else ""}

    def link(**changes) -> str:
        """Link da própria tela mantendo os filtros (e a ordem) atuais."""
        params = {**filters, "ordem": ordem, "dir": "desc" if descending else "asc", **changes}
        return "/auditoria?" + urlencode({k: v for k, v in params.items() if v})

    def sort_link(column: str) -> str:  # clicar de novo na mesma coluna inverte
        if column == ordem:
            return link(ordem=column, dir="asc" if descending else "desc", lead=lead)
        return link(ordem=column, dir="desc" if column in ("entrou", "recebeu", "respondeu") else "asc", lead=lead)

    return templates.TemplateResponse(request, "auditoria.html", {
        "tenant": tenant, "user": user,
        "active_nav": "dashboard" if features.has(tenant, "auditoria_no_dashboard") else "auditoria", "periodo": periodo, "periods": PERIODS,
        "q": q, "leads": leads, "total": total, "summaries": summaries, "limit": LIST_LIMIT,
        "people": people, "vendedor": vendedor, "origem": origem, "origins": list(PLATFORM_LABEL.items()),
        "situacao": situacao, "situations": SITUATIONS, "de": filters["de"], "ate": filters["ate"],
        "columns": COLUMNS, "ordem": ordem, "descending": descending, "link": link, "sort_link": sort_link,
        "has_filters": bool(vendedor or origem or situacao), "event_kinds": EVENT_KINDS,
        "platform_of": lambda l: PLATFORM_LABEL[platforms.get(l.id) or resolve_platform(l.attribution)],
        "selected": selected, "timeline": _timeline(selected) if selected else [],
        "owner_history": ownership.history(selected) if selected else [],
        "selected_platform": PLATFORM_LABEL[resolve_platform(selected.attribution)] if selected else "",
    })
