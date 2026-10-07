"""Follow-up e prazos: deixa claro quem está parado e quem precisa de um novo contato.

- ⏰ Parado na etapa: cada etapa tem um prazo (PipelineStage.sla_hours). Passou do prazo sem
  o lead mudar de etapa -> o card fica vermelho (perto do prazo, amarelo). É o empurrão pro
  time atualizar o Pipeline.
- 📞 Follow-up: a última mensagem foi NOSSA e o cliente não responde há mais de
  Tenant.followup_hours -> hora de chamar de novo. Conta as tentativas sem resposta.
- 📅 Próximo contato (opcional por cliente, Tenant.require_next_step): leads em Qualificado /
  Negociando precisam de um próximo contato agendado; sem agendamento ou com a data vencida
  aparecem como atrasados.
- 💰 Valor enviado: mensagem do time com preço (R$ 1.250,00 / 600,00 / 350 reais) leva o lead
  pra "Negociando" e guarda o valor (já vem preenchido quando marcar Ganho)."""

from __future__ import annotations

import datetime
import re
from typing import Optional

from sqlalchemy import and_, or_

from app.models import Lead, Message, PipelineStage, Tenant
from app.services import funnel

NEAR = 0.75  # a partir de 75% do prazo: amarelo

# prazos padrão (horas) pra clientes novos — venda mais longa (serviços etc.)
DEFAULT_SLA = {"em atendimento": 24, "qualificado": 48, "negociando": 72}
# autopeças fecha rápido (Nova Viseu): negociação em ~24h
FAST_SLA = {"em atendimento": 12, "qualificado": 12, "negociando": 24}
FAST_TENANTS = {"novaviseu": 12}  # subdomínio -> horas pro follow-up

_PRICE = re.compile(
    r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)"  # R$ 1.250,00 / R$350
    r"|(\d{1,3}(?:\.\d{3})*,\d{2})(?!\d)"  # 600,00 / 1.250,00
    r"|(\d+(?:,\d{1,2})?)\s*reais",  # 350 reais
    re.IGNORECASE,
)


def find_price(text: str) -> Optional[float]:
    """Maior valor em reais citado na mensagem (None se não tem preço)."""
    values = []
    for m in _PRICE.finditer(text or ""):
        raw = next(g for g in m.groups() if g)
        try:
            value = float(raw.replace(".", "").replace(",", "."))
        except ValueError:
            continue
        if value >= 1:
            values.append(value)
    return max(values) if values else None


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _open(stage: Optional[PipelineStage]) -> bool:
    return stage is not None and not stage.is_won and not stage.is_lost


def needs_next_step(tenant: Tenant, stage: Optional[PipelineStage], stages: list) -> bool:
    if not tenant.require_next_step or not _open(stage):
        return False
    return stage.order >= funnel.levels(stages)["qualified"]


def state(lead: Lead) -> dict:
    """Selos do card/conversa. Vazio pra grupos e negócios fechados."""
    stage = lead.stage
    out = {"stale": None, "followup": None, "next": None}
    if lead.is_group or not _open(stage):
        return out
    now = _now()
    if stage.sla_hours and lead.stage_entered_at:
        spent = now - lead.stage_entered_at
        limit = datetime.timedelta(hours=stage.sla_hours)
        if spent >= limit * NEAR:
            out["stale"] = {"for": spent, "late": spent >= limit, "stage": stage.name}
    tenant = lead.tenant
    ours_last = lead.last_outbound_at and (lead.last_inbound_at is None or lead.last_outbound_at > lead.last_inbound_at)
    if ours_last and now - lead.last_outbound_at >= datetime.timedelta(hours=tenant.followup_hours or 24):
        out["followup"] = {"for": now - lead.last_outbound_at, "attempts": max(lead.unanswered_outs or 1, 1)}
    if tenant.require_next_step and needs_next_step(tenant, stage, funnel._stages_cached(lead)):
        if lead.next_action_at is None:
            out["next"] = {"missing": True}
        else:
            out["next"] = {"at": lead.next_action_at, "late": lead.next_action_at < now,
                           "today": lead.next_action_at.date() <= now.date(), "note": lead.next_action_note}
    return out


# ---------- filtros (SQL) pra listas: Pipeline "só atrasados", Inbox, visão da equipe ----------

def stale_condition(stages: list):
    now = _now()
    parts = [
        and_(Lead.stage_id == st.id, Lead.stage_entered_at < now - datetime.timedelta(hours=st.sla_hours))
        for st in stages
        if st.sla_hours and not st.is_won and not st.is_lost
    ]
    return or_(*parts) if parts else Lead.id.is_(None)


def followup_condition(tenant: Tenant, stages: list):
    open_ids = [st.id for st in stages if not st.is_won and not st.is_lost]
    return and_(
        Lead.is_group.is_(False),
        Lead.stage_id.in_(open_ids),
        Lead.last_outbound_at.isnot(None),
        or_(Lead.last_inbound_at.is_(None), Lead.last_outbound_at > Lead.last_inbound_at),
        Lead.last_outbound_at < _now() - datetime.timedelta(hours=tenant.followup_hours or 24),
    )


def next_step_condition(tenant: Tenant, stages: list):
    """Sem próximo contato agendado, ou agendado pra hoje/antes (só se o cliente usa)."""
    if not tenant.require_next_step:
        return Lead.id.is_(None)
    lv = funnel.levels(stages)
    ids = [st.id for st in stages if not st.is_won and not st.is_lost and st.order >= lv["qualified"]]
    end_of_today = _now().replace(hour=23, minute=59, second=59)
    return and_(Lead.stage_id.in_(ids), or_(Lead.next_action_at.is_(None), Lead.next_action_at <= end_of_today))


def late_condition(tenant: Tenant, stages: list):
    """"Só atrasados": parado na etapa, follow-up pendente ou próximo contato vencido."""
    return or_(stale_condition(stages), followup_condition(tenant, stages), next_step_condition(tenant, stages))


# ---------- valor enviado -> Negociando ----------

def on_outbound_text(db, lead: Lead, body: str) -> None:
    """Mensagem do time com preço: guarda o valor e leva o lead pra Negociando (se ainda não
    passou de lá)."""
    if lead.is_group:
        return
    value = find_price(body)
    if value is None:
        return
    lead.quoted_value, lead.quoted_at = value, _now()
    stages = funnel._stages(db, lead.tenant_id)
    target = funnel.stage_named(stages, funnel.NEGOTIATION_NAME, "Negociação", "Negociacao", "Oportunidade")
    stage = lead.stage
    if target is not None and _open(stage) and stage.order < target.order:
        funnel.set_stage(lead, target)
        from app.models import Conversation

        conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
        if conv is not None:
            shown = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            db.add(Message(conversation_id=conv.id, direction="note", body=f"💰 Valor enviado ao cliente (R$ {shown}): lead foi pra {target.name}"))
    db.add(lead)


def setup(db) -> None:
    """Prazos padrão (uma vez) e dados dos leads de antes desta versão."""
    for tenant in db.query(Tenant).all():
        stages = funnel._stages(db, tenant.id)
        if stages and not any(st.sla_hours for st in stages):
            fast = tenant.subdomain in FAST_TENANTS
            table = FAST_SLA if fast else DEFAULT_SLA
            for st in stages:
                st.sla_hours = table.get(funnel._plain(st.name), 0)
            if fast:
                tenant.followup_hours = FAST_TENANTS[tenant.subdomain]
    db.query(Lead).filter(Lead.stage_entered_at.is_(None)).update(
        {Lead.stage_entered_at: Lead.updated_at, Lead.updated_at: Lead.updated_at}, synchronize_session=False
    )
    db.query(Lead).filter(
        Lead.unanswered_outs.is_(None) | (Lead.unanswered_outs == 0),
        Lead.last_outbound_at.isnot(None),
        or_(Lead.last_inbound_at.is_(None), Lead.last_outbound_at > Lead.last_inbound_at),
    ).update({Lead.unanswered_outs: 1, Lead.updated_at: Lead.updated_at}, synchronize_session=False)
    db.commit()
