"""🤖 Passagem automática de etapa: o que antes ficava como sugestão (💡 "parece qualificado /
venda fechou / perdido") agora o CRM aplica sozinho, deixando uma nota na conversa dizendo o
porquê. O vendedor sempre pode corrigir mudando a etapa (e aí o CRM não insiste no mesmo tipo).

Cuidados:
- só avança (Qualificado nunca puxa pra trás quem já está em Negociando);
- Ganho precisa de valor: o do comprovante (conferido) ou o último preço que o time mandou;
  o cliente dizendo "pode separar" só fecha se ele já recebeu preço;
- Perdido pela fala do time não vale se a mesma mensagem tem preço ("não temos o original,
  mas temos o paralelo por R$ 300" é negociação, não perda);
- tipo que o vendedor já recusou (suggest_dismissed) não é aplicado de novo no mesmo negócio.

E a volta: lead Ganho/Perdido que manda mensagem com cara de pedido novo (dados do carro,
peça, pergunta de preço) volta pro atendimento: perdido -> "Novo"; ganho -> negócio novo
(a venda feita continua contando)."""

from __future__ import annotations

import datetime
import re
import unicodedata
from typing import Optional

from sqlalchemy.orm import object_session

from app.models import Conversation, Lead, Message

AUTO_KINDS = ("qualificado", "ganho", "perdido")
# usado pela revisão em massa (scripts/etapas.py): sem notas no meio da conversa, sem mandar
# conversão pro Meta/Google de venda antiga, e guardando o motivo de cada passo
QUIET = False
LOG: list = []


def _plain(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().split())


def _note(db, lead: Lead, text: str) -> None:
    if QUIET:
        LOG.append((lead.id, text))
        return
    conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
    if conv is not None:
        db.add(Message(conversation_id=conv.id, direction="note", body=text))


def _brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# perdas que o cliente costuma dizer negociando ("tá caro, faz por 300?", "preciso pra hoje"):
# continuam só como sugestão pro vendedor confirmar
SUGGEST_ONLY_LOSS = ("Preço", "Prazo de entrega / disponibilidade")
SHORT_ONLY_PHRASES = ("infelizmente nao",)  # genérica: só vale em mensagem curta ("infelizmente não 😕")


def apply(db, lead: Lead, kind: str, *, reason: str = "", amount: Optional[float] = None, why: str = "",
          outbound: bool = False, body: str = "", phrase: str = "") -> bool:
    """Aplica a etapa sozinho. Devolve True se mudou."""
    from app import segments
    from app.services import followup, funnel

    stage = lead.stage
    if lead.is_group or stage is None or stage.is_won or stage.is_lost:
        return False
    if kind in (lead.suggest_dismissed or "").split(","):
        return False  # o vendedor já disse que não era isso
    stages = funnel._stages(db, lead.tenant_id)
    excerpt = (body or "").strip().replace("\n", " ")[:120]
    if kind == "qualificado":
        target = funnel.stage_named(stages, "Qualificado", "Qualificados")
        if target is None or stage.order >= target.order:
            return False
        funnel.set_stage(lead, target)
        _note(db, lead, f"🤖 Etapa automática: {target.name} — {why or 'pelo que foi dito na conversa'}"
                        + (f": “{excerpt}”" if excerpt else ""))
    elif kind == "ganho":
        target = next((st for st in stages if st.is_won), None)
        value = amount or lead.quoted_value or 0
        if target is None or value <= 0:
            return False
        if not outbound and amount is None and not lead.quoted_value:
            return False  # "pode separar" sem nunca ter recebido preço: ainda não dá pra dizer que vendeu
        funnel.set_stage(lead, target)
        lead.deal_value = value
        lead.receipt_status = "confirmed" if amount else lead.receipt_status
        origin = "valor lido do comprovante" if amount else "último preço enviado"
        _note(db, lead, f"🤖 Etapa automática: {target.name} ({_brl(value)}, {origin}) — {why or 'venda fechada na conversa'}"
                        + (f": “{excerpt}”" if excerpt else "") + ". Se não foi isso, é só mudar a etapa.")
        if target.conversion_event_name and not QUIET:
            from app.services.conversions.dispatcher import dispatch_stage_conversion

            dispatch_stage_conversion(db, lead, target.conversion_event_name)
    elif kind == "perdido":
        target = next((st for st in stages if st.is_lost), None)
        if target is None or (outbound and followup.find_price(body)):
            return False
        if not outbound and (reason in SUGGEST_ONLY_LOSS or "?" in (body or "")):
            return False
        if phrase in SHORT_ONLY_PHRASES and len(_plain(body).split()) > 8:
            return False
        reasons = segments.SEGMENTS[segments.key_for_lead(lead)]["loss_reasons"]
        reason = reason if reason in reasons else ("Outro" if "Outro" in reasons else reasons[-1])
        funnel.set_stage(lead, target)
        lead.loss_reason, lead.loss_detail = reason, f"automático: “{excerpt}”"[:500]
        _note(db, lead, f"🤖 Etapa automática: {target.name} ({reason})"
                        + (f" — “{excerpt}”" if excerpt else "") + ". Se o cliente voltar a pedir, o lead volta sozinho pra Novo.")
    else:
        return False
    lead.suggest_kind = ""
    db.add(lead)
    return True


# ---------- cliente que volta com pedido novo ----------
_REQUEST_WORDS = (
    "peca", "pecas", "farol", "lanterna", "retrovisor", "parachoque", "para-choque", "para choque", "motor",
    "cambio", "porta", "capo", "paralama", "radiador", "pastilha", "amortecedor", "bomba", "embreagem", "vidro",
    "roda", "pneu", "banco", "painel", "grade", "spoiler", "tampa", "orcamento", "cotacao", "cotar", "voces tem",
    "vcs tem", "voce tem", "vc tem", "tem disponivel", "ainda tem", "preciso de", "procuro", "procurando",
    "estou precisando", "to precisando", "queria saber", "teria", "consegue", "carro", "modelo", "ano",
)


def looks_like_request(text: str, segment: str = "autopecas") -> bool:
    """Mensagem com cara de pedido novo: dados do carro, peça, pergunta de preço/estoque."""
    from app.services import followup, suggestions

    plain = _plain(text)
    if not plain:
        return False
    if followup.asks_price(text):
        return True
    hit = suggestions.detect(text, outbound=False, segment=segment)
    if hit and hit[0] == "qualificado":
        return True  # placa, modelo + ano, chassi, "aceita troca"...
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", plain) for w in _REQUEST_WORDS)


def reopen_if_request(db, lead: Lead, body: str, when: Optional[datetime.datetime] = None) -> Lead:
    """Lead fechado (ganho/perdido) mandou pedido novo: volta pro atendimento. Devolve o lead
    que fica com a conversa (o mesmo, ou o negócio novo se era uma venda)."""
    from app import segments
    from app.services import deals, funnel

    stage = lead.stage
    if lead.is_group or stage is None or not (stage.is_won or stage.is_lost):
        return lead
    if refund_if_asked(db, lead, body, outbound=False):
        return lead  # pediu o dinheiro de volta: não é pedido novo
    replied = stage.is_lost and lead.loss_reason == segments.NO_REPLY_REASON and not funnel.is_courtesy(body)
    if not replied and not looks_like_request(body, segments.key_for_lead(lead)):
        return lead
    if stage.is_won:  # a venda feita continua contando: o pedido novo vira outro negócio
        return deals.open_new(db, lead, by_customer=True)
    funnel.reopen_lost(db, lead, body, when)
    return lead


# ---------- negociação que morreu ----------
LOST_AFTER_DAYS = 7  # sem nenhuma mensagem (nem do cliente nem nossa) há 7 dias: negociação acabou


def lose_silent_leads(db, days: int = LOST_AFTER_DAYS, now: Optional[datetime.datetime] = None, commit: bool = True) -> list:
    """Leads em atendimento (Em atendimento em diante, em aberto) sem nenhuma mensagem há X dias
    vão pra Perdido. Motivo claro ("Não responde mais": o time mandou 2+ mensagens sem resposta)
    já vem preenchido; sem motivo claro fica em branco e o card mostra "⚠️ motivo a preencher".
    Devolve [(lead, motivo)]. "Lead sem conversa"/"Novo" (o time nem respondeu) ficam de fora."""
    from sqlalchemy import func

    from app import segments
    from app.models import PipelineStage, Tenant
    from app.services import funnel

    now = now or datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(days=days)
    last_msg = dict(db.query(Conversation.lead_id, func.max(Conversation.last_message_at)).group_by(Conversation.lead_id).all())
    done = []
    for tenant in db.query(Tenant).all():
        stages = funnel._stages(db, tenant.id)
        lost = next((st for st in stages if st.is_lost), None)
        service = funnel.in_service_stage(stages)
        if lost is None or service is None:
            continue
        active = [st.id for st in stages if not st.is_won and not st.is_lost and st.order >= service.order]
        reasons = segments.SEGMENTS[segments.key_for(tenant)]["loss_reasons"]
        for lead in db.query(Lead).filter(
            Lead.tenant_id == tenant.id, Lead.stage_id.in_(active), Lead.is_group.is_(False),
            Lead.deleted_at.is_(None), Lead.archived_at.is_(None), Lead.tag != "outro",
        ):
            last = last_msg.get(lead.id)
            if last is None or last >= cutoff:
                continue
            ours_last = lead.last_outbound_at and (lead.last_inbound_at is None or lead.last_outbound_at > lead.last_inbound_at)
            reason = segments.NO_REPLY_REASON if ours_last and (lead.unanswered_outs or 0) >= 2 and segments.NO_REPLY_REASON in reasons else ""
            funnel.set_stage(lead, lost)
            lead.loss_reason = reason
            lead.loss_detail = f"automático: sem mensagens há {(now - last).days} dias"
            _note(db, lead, f"🤖 Etapa automática: {lost.name} — sem mensagens há {(now - last).days} dias"
                            + (f" ({reason})" if reason else ". ⚠️ Preencha o motivo da perda.")
                            + " Se o cliente voltar a pedir, o lead volta sozinho pra Novo.")
            done.append((lead, reason))
    if commit:
        db.commit()
    return done


# ---------- venda devolvida ----------
_REFUND_IN = (
    "dinheiro de volta", "devolver o dinheiro", "devolver meu dinheiro", "devolver o valor", "devolucao do dinheiro",
    "devolucao do valor", "devolucao do meu", "estorno", "estornar", "estorna", "reembolso", "reembolsar",
    "cancelar a compra", "cancelar o pedido", "cancelar minha compra",
)
_REFUND_OUT = (
    "estorno feito", "estorno realizado", "estornado", "estornamos", "reembolso feito", "reembolso realizado",
    "reembolsado", "devolvemos o valor", "devolvi o valor", "valor devolvido", "dinheiro devolvido",
    "fizemos a devolucao", "pix de devolucao", "compra cancelada", "pedido cancelado",
)


def is_refund(text: str, outbound: bool) -> bool:
    plain = _plain(text)
    if "troca" in plain and not outbound and not any(w in plain for w in ("dinheiro", "estorno", "reembolso")):
        return False  # "não serviu, quero trocar": troca mantém a venda
    return any(re.search(rf"(?<![a-z]){re.escape(p)}", plain) for p in (_REFUND_OUT if outbound else _REFUND_IN))


def refund_if_asked(db, lead: Lead, body: str, outbound: bool) -> bool:
    """Venda (Ganho) em que o cliente pediu o dinheiro de volta (ou o time confirmou o estorno):
    vai pra Perdido com o motivo "Devolução / reembolso" e sai do faturamento."""
    from app import segments
    from app.services import funnel

    stage = lead.stage
    if lead.is_group or stage is None or not stage.is_won or not is_refund(body, outbound):
        return False
    if "perdido" in (lead.suggest_dismissed or "").split(","):
        return False
    lost = next((st for st in funnel._stages(db, lead.tenant_id) if st.is_lost), None)
    if lost is None:
        return False
    value = f" de {_brl(lead.deal_value)}" if lead.deal_value else ""
    funnel.set_stage(lead, lost)
    lead.loss_reason = segments.REFUND_REASON
    lead.loss_detail = f"automático: “{(body or '').strip()[:200]}”"
    _note(db, lead, f"🤖 Etapa automática: {lost.name} ({segments.REFUND_REASON}) — a venda{value} saiu do faturamento"
                    f": “{(body or '').strip()[:120]}”. Se foi só troca, volte pra Ganho.")
    db.add(lead)
    return True


def lead_session(lead: Lead):
    return object_session(lead)
