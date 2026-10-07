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
    for st in db.query(PipelineStage).filter(PipelineStage.is_lost.is_(False)):
        db.query(Lead).filter(Lead.stage_id == st.id, or_(Lead.reached_order.is_(None), Lead.reached_order < st.order)).update(
            {Lead.reached_order: st.order, Lead.updated_at: Lead.updated_at}, synchronize_session=False
        )
    db.commit()
