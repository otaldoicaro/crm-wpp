"""Funil comercial: Leads > Qualificados > Negociações > Vendas.

Cada lead guarda a etapa mais avançada que já alcançou (Lead.reached_order), então quem
negociou e depois foi perdido continua contando como negociação."""

from __future__ import annotations

import logging
import unicodedata
from typing import Optional

from sqlalchemy import or_

from app.models import Lead, PipelineStage, Tenant

logger = logging.getLogger("funnel")

NEGOTIATION_NAME = "Negociando"
NO_CHAT_NAME = "Lead sem conversa"  # 1ª etapa: chegou (formulário/link) e ainda não falou no WhatsApp


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().strip()


def stage_named(stages: list, *names: str) -> Optional[PipelineStage]:
    wanted = {_plain(n) for n in names}
    return next((s for s in stages if _plain(s.name) in wanted), None)


def set_stage(lead: Lead, stage: PipelineStage) -> None:
    """Muda a etapa e lembra a mais avançada (perdido não conta como avanço)."""
    lead.stage_id = stage.id
    if not stage.is_lost and stage.order > (lead.reached_order or 0):
        lead.reached_order = stage.order


def _stages(db, tenant_id: str) -> list:
    return db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant_id).order_by(PipelineStage.order).all()


def entry_stage(db, tenant_id: str, with_chat: bool) -> Optional[PipelineStage]:
    """Etapa de um lead novo: sem conversa ainda -> "Lead sem conversa"; já falando -> "Novo"."""
    stages = _stages(db, tenant_id)
    no_chat = stage_named(stages, NO_CHAT_NAME)
    if not with_chat and no_chat:
        return no_chat
    return stage_named(stages, "Novo") or next(
        (s for s in stages if s is not no_chat and not s.is_won and not s.is_lost), stages[0] if stages else None
    )


def is_automatic(stage) -> bool:
    """Etapas de 1º contato ("Lead sem conversa" e "Novo"): só o CRM coloca leads nelas (quando o
    lead chega); ninguém move um lead pra lá na mão."""
    return stage is not None and _plain(stage.name) in (_plain(NO_CHAT_NAME), "novo")


AUTOMATIC_MSG = "“Lead sem conversa” e “Novo” são só pra leads que acabaram de chegar: o CRM coloca e tira sozinho."


def in_service_stage(stages: list) -> Optional[PipelineStage]:
    return stage_named(stages, "Em atendimento", "Atendimento", "Em contato")


def on_message(db, lead: Lead, outbound: bool) -> None:
    """Mensagem na conversa:
    - o time mandou (CRM ou celular): "Lead sem conversa"/"Novo" -> "Em atendimento";
    - o cliente mandou: "Lead sem conversa" -> "Novo" (fica pro time fazer o 1º contato).
    Lead que já está mais adiante no funil não volta."""
    stage = lead.stage
    if stage is None or lead.is_group:
        return
    stages = _stages(db, lead.tenant_id)
    novo = entry_stage(db, lead.tenant_id, with_chat=True)
    first_contact = {_plain(NO_CHAT_NAME)} | ({_plain(novo.name)} if novo else set())
    if _plain(stage.name) not in first_contact:
        return
    target = in_service_stage(stages) if outbound else (novo if _plain(stage.name) == _plain(NO_CHAT_NAME) else None)
    if target is not None and target.id != stage.id:
        set_stage(lead, target)
        db.add(lead)


def move_answered_to_service(db) -> int:
    """Leads em "Lead sem conversa"/"Novo" que o time já respondeu vão pra "Em atendimento"
    (assim "Novo" fica só com quem ainda espera o 1º contato)."""
    moved = 0
    for tenant in db.query(Tenant).all():
        stages = _stages(db, tenant.id)
        target = in_service_stage(stages)
        early = [s for s in (stage_named(stages, NO_CHAT_NAME), stage_named(stages, "Novo")) if s is not None]
        if target is None or not early:
            continue
        for lead in db.query(Lead).filter(
            Lead.tenant_id == tenant.id, Lead.stage_id.in_([s.id for s in early]),
            Lead.first_response_at.isnot(None), Lead.is_group.is_(False),
        ):
            set_stage(lead, target)
            moved += 1
    db.commit()
    if moved:
        logger.info("funil: %s leads já respondidos foram pra Em atendimento", moved)
    return moved


def levels(stages: list) -> dict:
    """Ordem mínima de cada degrau do funil. Sem etapa "Qualificado"/"Negociando" com esse
    nome, usa a 1ª etapa com evento de conversão / a etapa antes de Ganho."""
    won = next((s for s in stages if s.is_won), None)
    qualified = stage_named(stages, "Qualificado", "Qualificados") or next(
        (s for s in stages if s.conversion_event_name and not s.is_won), None
    )
    negotiation = stage_named(stages, NEGOTIATION_NAME, "Negociação", "Negociacao", "Oportunidade")
    return {
        "qualified": qualified.order if qualified else (won.order if won else 10**6),
        "negotiation": negotiation.order if negotiation else (won.order if won else 10**6),
        "won_ids": {s.id for s in stages if s.is_won},
    }


def summary(leads: list, stages: list) -> dict:
    """Números do funil + taxa de passagem de cada degrau."""
    lv = levels(stages)
    total = len(leads)
    qualified = sum(1 for l in leads if (l.reached_order or 0) >= lv["qualified"] or l.stage_id in lv["won_ids"])
    negotiating = sum(1 for l in leads if (l.reached_order or 0) >= lv["negotiation"] or l.stage_id in lv["won_ids"])
    won = sum(1 for l in leads if l.stage_id in lv["won_ids"])

    def rate(part, whole):
        return round(100 * part / whole, 1) if whole else None

    return {
        "steps": [
            {"label": "Leads", "count": total, "rate": None, "rate_label": ""},
            {"label": "Qualificados", "count": qualified, "rate": rate(qualified, total), "rate_label": "dos leads"},
            {"label": "Negociações", "count": negotiating, "rate": rate(negotiating, qualified), "rate_label": "dos qualificados"},
            {"label": "Vendas", "count": won, "rate": rate(won, negotiating), "rate_label": "das negociações"},
        ],
        "overall": rate(won, total),
    }


def is_qualified(stages: list):
    """Função lead -> bool (pro cruzamento com o tráfego pago)."""
    lv = levels(stages)
    return lambda lead: (lead.reached_order or 0) >= lv["qualified"] or lead.stage_id in lv["won_ids"]


def setup(db) -> None:
    """No start: cria a etapa "Negociando" (depois de Qualificado) em quem não tem, e calcula
    a etapa mais avançada dos leads antigos (= a atual, se não estiver perdido)."""
    for tenant in db.query(Tenant).all():
        stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
        if not stages or stage_named(stages, NEGOTIATION_NAME, "Negociação", "Negociacao", "Oportunidade"):
            continue
        qualified = stage_named(stages, "Qualificado", "Qualificados")
        if qualified is None:
            continue
        for st in stages:
            if st.order > qualified.order:
                st.order += 1
        db.add(PipelineStage(tenant_id=tenant.id, name=NEGOTIATION_NAME, order=qualified.order + 1))
        logger.info("funil: etapa %s criada em %s", NEGOTIATION_NAME, tenant.name)
        db.flush()
    db.commit()
    for tenant in db.query(Tenant).all():
        _create_no_chat_stage(db, tenant)
    for st in db.query(PipelineStage).filter(PipelineStage.is_lost.is_(False)):
        db.query(Lead).filter(Lead.stage_id == st.id, or_(Lead.reached_order.is_(None), Lead.reached_order < st.order)).update(
            {Lead.reached_order: st.order, Lead.updated_at: Lead.updated_at}, synchronize_session=False
        )
    db.commit()


def _create_no_chat_stage(db, tenant: Tenant) -> None:
    """Cria "Lead sem conversa" antes de todas as etapas (uma vez só) e põe nela os leads em
    aberto que nunca trocaram mensagem (em Novo/Em atendimento; quem já avançou fica onde está)."""
    from app.models import Conversation

    stages = _stages(db, tenant.id)
    if not stages or stage_named(stages, NO_CHAT_NAME):
        return
    for st in stages:
        st.order += 1
    # a etapa mais avançada dos leads acompanha a renumeração
    db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.reached_order.isnot(None)).update(
        {Lead.reached_order: Lead.reached_order + 1, Lead.updated_at: Lead.updated_at}, synchronize_session=False
    )
    no_chat = PipelineStage(tenant_id=tenant.id, name=NO_CHAT_NAME, order=0)
    db.add(no_chat)
    db.flush()
    qualified = stage_named(stages, "Qualificado", "Qualificados")
    early = [s.id for s in stages if not s.is_won and not s.is_lost and (qualified is None or s.order < qualified.order)]
    with_chat = db.query(Conversation.lead_id).filter(Conversation.tenant_id == tenant.id)
    moved = (
        db.query(Lead)
        .filter(
            Lead.tenant_id == tenant.id, Lead.is_group.is_(False), Lead.deleted_at.is_(None),
            Lead.stage_id.in_(early), Lead.id.notin_(with_chat),
        )
        .update({Lead.stage_id: no_chat.id, Lead.updated_at: Lead.updated_at}, synchronize_session=False)
    )
    db.commit()
    logger.info("funil: etapa %s criada em %s (%s leads sem conversa movidos)", NO_CHAT_NAME, tenant.name, moved)
