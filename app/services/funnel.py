"""Funil comercial: Leads > Qualificados > Negociações > Vendas.

Cada lead guarda a etapa mais avançada que já alcançou (Lead.reached_order), então quem
negociou e depois foi perdido continua contando como negociação."""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from typing import Optional

from sqlalchemy import or_

from app import segments
from app.models import Lead, PipelineStage, Tenant

logger = logging.getLogger("funnel")

NEGOTIATION_NAME = "Negociando"
# a mesma etapa com outros nomes; "Agendamento" = recurso "agendamentos" (visita marcada na loja)
NEGOTIATION_NAMES = (NEGOTIATION_NAME, "Negociação", "Negociacao", "Oportunidade", "Agendamento", "Proposta enviada")
SCHEDULE_NAME = "Agendamento"
COLD_NAME = "Lead frio"  # recurso "lead_frio": o cliente parou de responder
NO_CHAT_NAME = "Lead sem conversa"  # 1ª etapa: chegou (formulário/link) e ainda não falou no WhatsApp


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().strip()


def stage_named(stages: list, *names: str) -> Optional[PipelineStage]:
    wanted = {_plain(n) for n in names}
    return next((s for s in stages if _plain(s.name) in wanted), None)


def negotiation_stage(stages: list) -> Optional[PipelineStage]:
    return stage_named(stages, *NEGOTIATION_NAMES)


def lost_stage(stages: list, lead: Optional[Lead] = None) -> Optional[PipelineStage]:
    """Etapa de perdido pra este lead. Funil com mais de uma (ex: ramo agência: "Lead perdido" e
    "Proposta perdida"): quem já chegou na etapa de oportunidade (Proposta enviada/Negociando) vai pra
    última; quem não chegou, pra primeira."""
    lost = [s for s in stages if s.is_lost]
    if len(lost) < 2 or lead is None:
        return lost[0] if lost else None
    negotiation = negotiation_stage(stages)
    reached = max(lead.reached_order or 0, lead.stage.order if lead.stage and not lead.stage.is_lost else 0)
    if negotiation is not None and reached >= negotiation.order:
        return lost[-1]
    return lost[0]


def set_stage(lead: Lead, stage: PipelineStage) -> None:
    """Muda a etapa e lembra a mais avançada (perdido não conta como avanço) e quando fechou."""
    closing = stage.is_won or stage.is_lost
    if closing and (lead.closed_at is None or lead.stage_id != stage.id):
        lead.closed_at = datetime.datetime.utcnow()
    elif not closing:
        lead.closed_at = None
    if lead.stage_id != stage.id or lead.stage_entered_at is None:
        lead.stage_entered_at = datetime.datetime.utcnow()  # conta o "há X tempo nesta etapa"
    changed = lead.stage_id != stage.id
    lead.stage_id = stage.id
    if stage.is_won and not lead.tag:
        lead.tag = "cliente"  # comprou: vira Cliente (a próxima compra fica com o mesmo vendedor, sem rodízio)
    if not stage.is_lost and stage.order > (lead.reached_order or 0):
        lead.reached_order = stage.order
    if changed:
        from app.services.conversions import queue

        queue.on_stage(lead, stage)  # 📤 oportunidade/venda pro Meta (só se o cliente ligou e conferiu o pixel)


def won_are_customers(db) -> int:
    """Leads em Ganho ainda marcados como "Lead" viram "Cliente" (uma vez, no start)."""
    won = [s.id for s in db.query(PipelineStage).filter(PipelineStage.is_won.is_(True))]
    if not won:
        return 0
    n = db.query(Lead).filter(Lead.stage_id.in_(won), Lead.tag == "", Lead.is_group.is_(False)).update(
        {Lead.tag: "cliente", Lead.updated_at: Lead.updated_at}, synchronize_session=False)
    db.commit()
    return n


def _stages(db, tenant_id: str) -> list:
    return db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant_id).order_by(PipelineStage.order).all()


def _stages_cached(lead: Lead) -> list:
    """Etapas do cliente do lead (lidas uma vez por sessão do banco)."""
    from sqlalchemy.orm import object_session

    session = object_session(lead)
    cache = session.info.setdefault("stages_by_tenant", {})
    if lead.tenant_id not in cache:
        cache[lead.tenant_id] = _stages(session, lead.tenant_id)
    return cache[lead.tenant_id]


def entry_stage(db, tenant_id: str, with_chat: bool) -> Optional[PipelineStage]:
    """Etapa de um lead novo: sem conversa ainda -> "Lead sem conversa"; já falando -> "Novo"."""
    stages = _stages(db, tenant_id)
    no_chat = stage_named(stages, NO_CHAT_NAME)
    if not with_chat and no_chat:
        return no_chat
    return stage_named(stages, "Novo") or next(
        (s for s in stages if s is not no_chat and not s.is_won and not s.is_lost), stages[0] if stages else None
    )


NO_REPLY_REASON = segments.NO_REPLY_REASON
LOSS_REASONS = segments.SEGMENTS[segments.DEFAULT_SEGMENT]["loss_reasons"]  # o de cada cliente: segments.loss_reasons()
FOLLOW_UPS_BEFORE_NO_REPLY = 2  # mensagens do time sem resposta antes de poder marcar "Não responde mais"


def check_loss(db, lead: Lead, stage: PipelineStage, reason: str) -> Optional[str]:
    """Erro (texto pra mostrar) se não dá pra marcar como perdido com esse motivo, senão None."""
    if not stage.is_lost:
        return None
    if reason not in segments.SEGMENTS[segments.key_for_lead(lead)]["loss_reasons"]:
        return "Escolha o motivo da perda."
    if reason == NO_REPLY_REASON:
        from app.models import Conversation, Message

        query = (
            db.query(Message.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .filter(Conversation.lead_id == lead.id, Message.direction == "out", ~Message.body.like("⚠️ Não enviada%"))
        )
        if lead.last_inbound_at:
            query = query.filter(Message.created_at > lead.last_inbound_at)
        if query.count() < FOLLOW_UPS_BEFORE_NO_REPLY:
            return (
                f"“{NO_REPLY_REASON}” só depois de pelo menos {FOLLOW_UPS_BEFORE_NO_REPLY} follow-ups sem resposta "
                "(mensagens do time depois da última mensagem do cliente)."
            )
    return None


def is_automatic(stage) -> bool:
    """Etapas de 1º contato ("Lead sem conversa" e "Novo"): só o CRM coloca leads nelas (quando o
    lead chega); ninguém move um lead pra lá na mão."""
    return stage is not None and _plain(stage.name) in (_plain(NO_CHAT_NAME), "novo")


AUTOMATIC_MSG = "“Lead sem conversa” e “Novo” são só pra leads que acabaram de chegar: o CRM coloca e tira sozinho."


_COURTESY = ("obrigad", "brigad", "valeu", "vlw", "ok", "okay", "blz", "beleza", "tranquilo", "show", "abraco",
             "abs", "tmj", "certo", "entendi", "combinado", "joia", "otimo", "perfeito", "disponha", "fechou", "falou",
             "nada", "problema", "bom", "boa", "ta", "tudo", "sem", "de", "muito", "mto", "mt", "agradeco", "amigo",
             "irmao", "mano", "chefe", "entao", "sim", "e", "pra", "voce", "vc", "tbm", "tambem", "kk", "kkk", "rs")
_COURTESY_CORE = ("obrigad", "brigad", "valeu", "vlw", "ok", "okay", "blz", "beleza", "tranquilo", "show", "tmj",
                  "certo", "entendi", "combinado", "joia", "agradeco", "disponha", "fechou", "falou", "abraco", "abs")


def is_courtesy(text: str) -> bool:
    """ "ok, obrigado", "valeu!", "👍": só educação, não é o cliente voltando a negociar.
    Pergunta ("?") ou qualquer palavra de assunto (peça, carro, preço...) já não é."""
    plain = _plain(text)
    words = re.findall(r"[a-z0-9]+", plain)
    if not words:
        return "?" not in plain  # só emoji / figurinha
    if "?" in plain or len(words) > 6:
        return False
    return (all(w.startswith(_COURTESY) for w in words)
            and any(w.startswith(_COURTESY_CORE) for w in words))


def reopen_lost(db, lead: Lead, body: str, when: Optional[datetime.datetime] = None) -> bool:
    """Cliente de um lead PERDIDO voltou a falar (antes dos 7 dias do negócio novo): o lead volta
    pra "Novo" pro time atender. "Ok, obrigado" não conta. Devolve True se voltou."""
    stage = lead.stage
    if stage is None or not stage.is_lost or lead.is_group or is_courtesy(body):
        return False
    novo = entry_stage(db, lead.tenant_id, with_chat=True)
    if novo is None:
        return False
    set_stage(lead, novo)
    if when is not None:
        lead.stage_entered_at = when
    from app.models import Conversation, Message

    conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
    if conv is not None:
        db.add(Message(conversation_id=conv.id, direction="note",
                       body=f"↩️ Cliente voltou a falar: saiu de Perdido ({lead.loss_reason or 'sem motivo'}) e voltou pra {novo.name}"))
    db.add(lead)
    return True


def in_service_stage(stages: list) -> Optional[PipelineStage]:
    return stage_named(stages, "Em atendimento", "Atendimento", "Em contato")


def on_message(db, lead: Lead, outbound: bool) -> None:
    """Mensagem na conversa:
    - o time mandou (CRM ou celular): "Lead sem conversa"/"Novo" -> "Em atendimento";
    - o cliente mandou: "Lead sem conversa" -> "Novo" (fica pro time fazer o 1º contato);
      "Lead frio" -> "Em atendimento".
    Lead que já está mais adiante no funil não volta."""
    stage = lead.stage
    if stage is None or lead.is_group:
        return
    stages = _stages(db, lead.tenant_id)
    if not outbound and _plain(stage.name) == _plain(COLD_NAME):  # ❄️ o cliente voltou a falar
        target = in_service_stage(stages)
        if target is not None:
            set_stage(lead, target)
            db.add(lead)
        return
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
    negotiation = negotiation_stage(stages)
    return {
        "qualified": qualified.order if qualified else (won.order if won else 10**6),
        "negotiation": negotiation.order if negotiation else (won.order if won else 10**6),
        "won_ids": {s.id for s in stages if s.is_won},
    }


def summary(leads: list, stages: list, tenant=None) -> dict:
    """Números do funil + taxa de passagem de cada degrau."""
    lv = levels(stages)
    total = len(leads)
    is_q, is_opp = is_qualified(stages, tenant), is_opportunity(stages)
    qualified = sum(1 for l in leads if is_q(l))
    negotiating = sum(1 for l in leads if is_opp(l))
    won = sum(1 for l in leads if l.stage_id in lv["won_ids"])

    def rate(part, whole):
        return round(100 * part / whole, 1) if whole else None

    return {
        "steps": [
            {"label": "Leads", "count": total, "rate": None, "rate_label": ""},
            {"label": "Qualificados", "count": qualified, "rate": rate(qualified, total), "rate_label": "dos leads"},
            {"label": "Propostas" if _agency(tenant) else "Oportunidades", "count": negotiating,
             "rate": rate(negotiating, qualified), "rate_label": "dos qualificados"},
            {"label": "Vendas", "count": won, "rate": rate(won, negotiating), "rate_label": "das oportunidades"},
        ],
        "overall": rate(won, total),
    }


def is_opportunity(stages: list):
    """Função lead -> bool: chegou em Negociando (recebeu preço) em algum momento, ou ganhou."""
    lv = levels(stages)
    return lambda lead: (lead.reached_order or 0) >= lv["negotiation"] or lead.stage_id in lv["won_ids"]


def _agency(tenant) -> bool:
    from app import features

    return tenant is not None and features.has(tenant, "dash_agencia")


def is_qualified(stages: list, tenant=None):
    """Função lead -> bool (pro cruzamento com o tráfego pago). Recurso "dash_agencia": qualificado é quem
    respondeu faturamento/investimento acima do mínimo (lead_profile.money_qualified) ou já chegou na proposta."""
    lv = levels(stages)
    if _agency(tenant):
        from app.services import lead_profile

        return lambda lead: (lead_profile.money_qualified(lead) or (lead.reached_order or 0) >= lv["negotiation"]
                             or lead.stage_id in lv["won_ids"])
    return lambda lead: (lead.reached_order or 0) >= lv["qualified"] or lead.stage_id in lv["won_ids"]


def setup(db) -> None:
    """No start: cria a etapa "Negociando" (depois de Qualificado) em quem não tem, e calcula
    a etapa mais avançada dos leads antigos (= a atual, se não estiver perdido)."""
    for tenant in db.query(Tenant).all():
        stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).order_by(PipelineStage.order).all()
        if not stages or negotiation_stage(stages):
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
        _sync_segment_stages(db, tenant)
        _create_no_chat_stage(db, tenant)
        apply_features(db, tenant)
    terminal = [st.id for st in db.query(PipelineStage).filter(PipelineStage.is_won | PipelineStage.is_lost)]
    db.query(Lead).filter(Lead.stage_id.in_(terminal), Lead.closed_at.is_(None)).update(
        {Lead.closed_at: Lead.updated_at, Lead.updated_at: Lead.updated_at}, synchronize_session=False
    )  # fechados antes desta versão: a data da última mudança é a melhor aproximação
    for st in db.query(PipelineStage).filter(PipelineStage.is_lost.is_(False)):
        db.query(Lead).filter(Lead.stage_id == st.id, or_(Lead.reached_order.is_(None), Lead.reached_order < st.order)).update(
            {Lead.reached_order: st.order, Lead.updated_at: Lead.updated_at}, synchronize_session=False
        )
    db.commit()


def apply_features(db, tenant: Tenant) -> None:
    """Etapas que dependem de recurso do cliente (no start e ao salvar ⚙ Prazos → Recursos):
    📅 "agendamentos": Negociando se chama Agendamento (e volta a Negociando se desligar);
    ❄️ "lead_frio": cria a etapa Lead frio antes de Ganho/Perdido; desligado, a etapa sai do funil
    (quem estava nela volta pra Em atendimento)."""
    from app import features

    stages = _stages(db, tenant.id)
    if not stages:
        return
    scheduling = features.has(tenant, "agendamentos")
    negotiation = negotiation_stage(stages)
    if negotiation is not None:
        if scheduling and _plain(negotiation.name) == _plain(NEGOTIATION_NAME):
            negotiation.name = SCHEDULE_NAME
        elif not scheduling and _plain(negotiation.name) == _plain(SCHEDULE_NAME):
            negotiation.name = NEGOTIATION_NAME
    if features.has(tenant, "lead_frio") and stage_named(stages, COLD_NAME) is None:
        closing = [st for st in stages if st.is_won or st.is_lost]
        order = min(st.order for st in closing) if closing else max(st.order for st in stages) + 1
        for st in stages:
            if st.order >= order:
                st.order += 1
        db.add(PipelineStage(tenant_id=tenant.id, name=COLD_NAME, order=order))
        logger.info("funil: etapa %s criada em %s", COLD_NAME, tenant.name)
    cold = stage_named(stages, COLD_NAME)
    if cold is not None and not features.has(tenant, "lead_frio"):
        # recurso desligado: a etapa sai do funil e quem estava nela volta pra Em atendimento
        # (a regra normal dos 7 dias parados cuida deles dali em diante)
        back = in_service_stage(stages)
        if back is None:
            db.commit()
            return
        moved = 0
        for lead in db.query(Lead).filter(Lead.stage_id == cold.id):
            set_stage(lead, back)
            moved += 1
        db.flush()
        db.delete(cold)
        for st in stages:
            if st.order > cold.order:
                st.order -= 1
        logger.info("funil: etapa %s removida em %s (%s leads voltaram pra %s)", COLD_NAME, tenant.name, moved, back.name)
    db.commit()


def _sync_segment_stages(db, tenant: Tenant) -> None:
    """Etapas que o ramo renomeou ou tirou (segments.py, "stage_renames" / "stage_removed"): ajusta os
    clientes criados antes. Os leads de uma etapa tirada vão pra etapa indicada."""
    seg = segments.segment_for(tenant)
    renames, removed = seg.get("stage_renames") or {}, seg.get("stage_removed") or {}
    added = seg.get("stage_added") or []
    if not renames and not removed and not added:
        return
    for old, new in renames.items():
        stages = _stages(db, tenant.id)
        stage = stage_named(stages, old)
        if stage is not None and stage_named(stages, new) is None:
            stage.name = new
            logger.info("funil: etapa %s virou %s em %s", old, new, tenant.name)
    db.flush()
    for old, target_name in removed.items():
        stages = _stages(db, tenant.id)
        stage, target = stage_named(stages, old), stage_named(stages, target_name)
        if stage is None or target is None:
            continue
        moved = db.query(Lead).filter(Lead.stage_id == stage.id).update(
            {Lead.stage_id: target.id, Lead.updated_at: Lead.updated_at}, synchronize_session=False)
        db.delete(stage)
        logger.info("funil: etapa %s tirada de %s (%s leads foram pra %s)", old, tenant.name, moved, target_name)
    db.flush()
    # etapas que o ramo ganhou ("stage_added"): entram logo depois da etapa anterior da lista do ramo
    wanted = seg.get("stages") or []
    for name in added:
        stages = _stages(db, tenant.id)
        spec = next((s for s in wanted if s[0] == name), None)
        if spec is None or not stages or stage_named(stages, name) is not None:
            continue
        index = wanted.index(spec)
        before = next((stage_named(stages, w[0]) for w in reversed(wanted[:index]) if stage_named(stages, w[0])), None)
        order = (before.order + 1) if before is not None else max(s.order for s in stages) + 1
        for st in stages:
            if st.order >= order:
                st.order += 1
        db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.reached_order >= order).update(
            {Lead.reached_order: Lead.reached_order + 1, Lead.updated_at: Lead.updated_at}, synchronize_session=False)
        new_stage = PipelineStage(tenant_id=tenant.id, name=name, conversion_event_name=spec[1],
                                  is_won=spec[2], is_lost=spec[3], order=order)
        db.add(new_stage)
        db.flush()
        logger.info("funil: etapa %s criada em %s", name, tenant.name)
        if spec[3]:  # perdido novo: quem já estava perdido vai pra etapa certa (lost_stage)
            stages = _stages(db, tenant.id)
            old_lost = [st.id for st in stages if st.is_lost and st.id != new_stage.id]
            moves = [(lead.id, target.id) for lead in db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.stage_id.in_(old_lost))
                     for target in [lost_stage(stages, lead)] if target is not None and target.id != lead.stage_id]
            for lead_id, stage_id in moves:
                db.query(Lead).filter(Lead.id == lead_id).update(
                    {Lead.stage_id: stage_id, Lead.updated_at: Lead.updated_at}, synchronize_session=False)
            logger.info("funil: %s leads perdidos depois da proposta foram pra %s em %s", len(moves), name, tenant.name)
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


def edit_stages(db, tenant: Tenant, existing: list, added: list) -> str:
    """🧭 Editor de etapas (⚙ Configurações → Funil e prazos), só deste cliente.
    existing: [(etapa, novo nome, ordem, id da etapa destino se for tirada ou "")]
    added: [(nome, ordem)] etapas novas (abertas: nem venda nem perda).
    Tirar uma etapa move os leads dela pra etapa escolhida. Devolve "" ou o erro."""
    stages = _stages(db, tenant.id)
    by_id = {s.id: s for s in stages}
    removing = {stage.id: target for stage, _name, _order, target in existing if target}
    for stage_id, target in removing.items():
        if target not in by_id or target in removing:
            return "Escolha pra onde vão os leads de cada etapa que você tirou (uma etapa que fica)."
        if is_automatic(by_id[stage_id]):
            return "“Lead sem conversa” e “Novo” são automáticas: não dá pra tirar."
    kept = [(stage, name, order) for stage, name, order, target in existing if not target]
    if not any(s.is_won for s, _n, _o in kept) or not any(s.is_lost for s, _n, _o in kept):
        return "O funil precisa de pelo menos uma etapa de venda e uma de perda."
    names = [(_plain(n) or _plain(s.name)) for s, n, _o in kept] + [_plain(n) for n, _o in added]
    if len(set(names)) != len(names):
        return "Tem duas etapas com o mesmo nome."
    for stage_id, target in removing.items():
        db.query(Lead).filter(Lead.stage_id == stage_id).update(
            {Lead.stage_id: target, Lead.updated_at: Lead.updated_at}, synchronize_session=False)
    old_order = {s.id: s.order for s in stages}
    rows = [(order, old_order[s.id], s, name) for s, name, order in kept]
    for i, (name, order) in enumerate(added):
        stage = PipelineStage(tenant_id=tenant.id, name=name[:80])
        db.add(stage)
        rows.append((order, 1000 + i, stage, name))
    db.flush()
    rows.sort(key=lambda r: (r[0], r[1]))
    for new_order, (_o, _old, stage, name) in enumerate(rows):
        if name.strip():
            stage.name = name.strip()[:80]
        stage.order = new_order
    # a etapa mais avançada de cada lead acompanha a nova ordem
    mapping = {old_order[s.id]: s.order for _o, _old, s, _n in rows if s.id in old_order}
    for removed_id, target in removing.items():
        mapping[old_order[removed_id]] = by_id[target].order
    leads = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.reached_order.isnot(None)).all()
    for lead in leads:
        old = lead.reached_order or 0
        prev = max((p for p in mapping if p <= old), default=None)
        db.query(Lead).filter(Lead.id == lead.id).update(
            {Lead.reached_order: mapping[prev] if prev is not None else 0, Lead.updated_at: Lead.updated_at},
            synchronize_session=False)
    for stage_id in removing:
        db.delete(by_id[stage_id])
    db.commit()
    logger.info("funil: etapas editadas em %s: %s", tenant.name, [s.name for _o, _old, s, _n in rows])
    return ""
