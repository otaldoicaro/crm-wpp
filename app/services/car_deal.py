"""Venda de carro (recursos "pagamento_parcelado" e "visita_negociando", ligados por cliente
em app/features.py — hoje: seminovos).

- Condição de pagamento lida da conversa: "60x 2.473,25" / "60x de R$ 2.473,25" (parcelas),
  "entrada de 6.700" / "R$ 10 mil de entrada" / "zero de entrada" / "sem entrada". Fica no lead
  (pay_mode, down_payment, installments, installment_value) e o vendedor corrige no topo da
  conversa. A parcela NÃO vira "valor enviado" (que é o preço do carro): sem isso, "60x 2.473,25"
  aparecia como se o carro custasse R$ 2.473,25.
- Visita marcada na loja (pelo cliente ou pelo time): o lead vai pra Negociando."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from app import features
from app.models import Conversation, Lead, Message

_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?"
_INSTALLMENTS = re.compile(rf"\b(?P<n>\d{{1,3}})\s*(?:x|vezes)\s*(?:de\s*)?(?:R\$\s*)?(?P<v>{_NUM})(?!\s*(?:km|mil\b))", re.I)
_DOWN = re.compile(
    rf"entrada\s*(?:de|:)?\s*(?:R\$\s*)?(?P<a>{_NUM})\s*(?P<am>mil\b)?"
    rf"|(?:R\$\s*)?(?P<b>{_NUM})\s*(?P<bm>mil\b)?\s*(?:reais\s*)?de\s*entrada",
    re.I,
)
_NO_DOWN = re.compile(r"\b(?:sem|zero\s+de|0\s+de|nada\s+de)\s+entrada\b", re.I)


def _plain(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().split())


def _money(raw: str, thousand: bool = False) -> Optional[float]:
    try:
        value = float(raw.replace(",", ".")) * 1000 if thousand else float(raw.replace(".", "").replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_terms(text: str) -> dict:
    """{"installments", "installment_value", "down_payment"} achados no texto (só as chaves achadas)."""
    found = {}
    m = _INSTALLMENTS.search(text or "")
    if m and 2 <= int(m.group("n")) <= 120:
        value = _money(m.group("v"))
        if value and value >= 50:
            found["installments"], found["installment_value"] = int(m.group("n")), value
    if _NO_DOWN.search(text or ""):
        found["down_payment"] = 0.0
    else:
        d = _DOWN.search(text or "")
        if d:
            raw, mil = (d.group("a"), d.group("am")) if d.group("a") else (d.group("b"), d.group("bm"))
            value = _money(raw, bool(mil))
            if value is not None and value < 100:
                value = None  # "entrada de 6": ambíguo (6 mil?), melhor o vendedor preencher
            if value:
                found["down_payment"] = value
    return found


def strip_terms(text: str) -> str:
    """Texto sem os trechos de parcela/entrada (pra procurar o preço do carro no resto)."""
    return _DOWN.sub(" ", _INSTALLMENTS.sub(" ", text or ""))


# visita marcada: frases de quem combinou de ir à loja (cliente) ou confirmou a visita (time)
VISIT_IN = ("vou passar ai", "passo ai", "vou ai na loja", "vou na loja", "vou ir ai", "vou ai ver", "to indo ai",
            "estou indo ai", "chego ai", "vou ai amanha", "vou ai hoje", "amanha eu passo", "amanha passo ai",
            "vou ver o carro ai", "vou ver pessoalmente", "marcado entao", "combinado entao", "fechado, vou ai")
VISIT_OUT = ("visita agendada", "agendei sua visita", "agendei a visita", "agendado pra", "agendada pra",
             "fica agendado", "ficou agendado", "esta agendado", "ta agendado", "te espero aqui", "te espero amanha",
             "te espero hoje", "te aguardo aqui", "te aguardo amanha", "te aguardo hoje", "te esperamos",
             "aguardamos sua visita", "aguardo sua visita", "pode vir ver", "combinado, te espero")


def visit_phrase(body: str, outbound: bool) -> Optional[str]:
    plain = _plain(body)
    for p in VISIT_OUT if outbound else VISIT_IN:
        if re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", plain):
            return p
    return None


def _note(db, lead: Lead, text: str) -> None:
    conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
    if conv is not None:
        db.add(Message(conversation_id=conv.id, direction="note", body=text))


def _brl(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def describe(lead: Lead) -> str:
    """"Entrada R$ 6.700,00 + 60x R$ 2.473,25" / "À vista" / ""."""
    if lead.pay_mode == "avista":
        return "À vista"
    parts = []
    if lead.down_payment is not None:
        parts.append(f"Entrada R$ {_brl(lead.down_payment)}" if lead.down_payment else "Sem entrada")
    if lead.installments:
        parts.append(f"{lead.installments}x" + (f" R$ {_brl(lead.installment_value)}" if lead.installment_value else ""))
    return " + ".join(parts)


def to_negotiation(db, lead: Lead, why: str) -> bool:
    """Leva pra Negociando (só avança; ganho/perdido ficam como estão)."""
    from app.services import funnel

    stage = lead.stage
    target = funnel.stage_named(funnel._stages(db, lead.tenant_id), funnel.NEGOTIATION_NAME, "Negociação", "Negociacao")
    if target is None or stage is None or stage.is_won or stage.is_lost or stage.order >= target.order:
        return False
    funnel.set_stage(lead, target)
    _note(db, lead, f"{why}: lead foi pra {target.name}")
    return True


def on_text(db, lead: Lead, body: str, outbound: bool) -> bool:
    """Chamado a cada mensagem com texto. True = a mensagem tinha condição de pagamento (aí o
    preço que ela cita é parcela/entrada, não o valor do carro)."""
    tenant = lead.tenant
    if lead.is_group or tenant is None:
        return False
    had_terms = False
    if features.has(tenant, "pagamento_parcelado"):
        terms = parse_terms(body)
        if terms:
            had_terms = True
            for key, value in terms.items():
                setattr(lead, key, value)
            if "installments" in terms:
                lead.pay_mode = "parcelado"
            if outbound:
                to_negotiation(db, lead, f"💳 Condição enviada ao cliente ({describe(lead)})")
    if features.has(tenant, "visita_negociando"):
        phrase = visit_phrase(body, outbound)
        if phrase:
            to_negotiation(db, lead, f"📅 Visita à loja combinada (“{phrase}”)")
    db.add(lead)
    return had_terms


def save_form(lead: Lead, pay_mode: str, down_payment: str, installments: str, installment_value: str, parse_brl) -> None:
    """Campos de pagamento do topo da conversa / ficha do lead."""
    lead.pay_mode = pay_mode if pay_mode in ("avista", "parcelado") else ""
    if lead.pay_mode == "avista":
        lead.down_payment = lead.installments = lead.installment_value = None
        return
    try:
        lead.down_payment = parse_brl(down_payment) if down_payment.strip() else None
    except ValueError:
        pass
    digits = re.sub(r"\D", "", installments or "")
    lead.installments = int(digits) if digits and 0 < int(digits) <= 120 else None
    try:
        lead.installment_value = (parse_brl(installment_value) or None) if installment_value.strip() else None
    except ValueError:
        pass
