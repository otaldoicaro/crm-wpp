"""🔥 Sinal de compra depois do preço: o cliente recebeu o preço e respondeu algo como "como faço?",
"como funciona?", "qual o pix?", "faz entrega?", "parcela?". É o lead mais quente que existe e
muitas vezes o vendedor manda o preço e some. Fica marcado no lead (Lead.interest_at / interest_text)
pra aparecer no filtro do Inbox "🔥 Quis comprar depois do preço" e numa etiqueta no card.

Só vale pra negócio em aberto, depois do 1º preço que o time mandou (Lead.quoted_at). A lista de
frases foi feita olhando o que os clientes que COMPRARAM escreveram depois do preço
(scripts/sinais_compra.py mostra isso nas vendas e marca as negociações antigas)."""

from __future__ import annotations

import datetime
import re
import unicodedata
from typing import Optional

from app.models import Lead

PHRASES = (
    # como comprar / pagar
    "como faco", "como faz", "como funciona", "como que faz", "como que funciona", "como pago", "como eu pago",
    "como compro", "como faco pra comprar", "como faco o pedido", "como fica o pagamento", "forma de pagamento",
    "qual o pix", "qual a chave", "chave pix", "manda o pix", "me passa o pix", "passa o pix", "manda a chave",
    "aceita cartao", "aceita pix", "no cartao", "no credito", "no debito", "parcela", "parcelar", "parcelado",
    "em quantas vezes", "divide", "dividir", "boleto", "link de pagamento", "manda o link", "parcelam", "parcelas",
    "aceitam cartao", "aceitam pix", "passa cartao", "passam cartao", "cartao de credito",
    # entrega / retirada
    "faz entrega", "fazem entrega", "entrega em", "voces entregam", "vcs entregam", "entregam em", "envia pra",
    "envia para", "manda pra", "frete", "quanto fica o frete", "qual o endereco", "onde fica", "onde voces ficam",
    "onde vcs ficam", "posso buscar", "posso retirar", "posso ir ai", "posso passar ai", "da pra buscar",
    "da pra retirar", "retirar hoje", "buscar hoje", "que horas fecha", "ate que horas", "horario de funcionamento",
    # garantia / fechar
    "tem garantia", "qual a garantia", "quanto tempo de garantia", "nota fiscal", "vou querer", "quero sim",
    "eu quero", "pode separar", "separa pra mim", "separa ai", "reserva pra mim", "fechado", "vou fechar",
    "pode mandar", "ainda tem", "ainda esta disponivel", "consegue fazer por", "faz por", "desconto",
    "melhor preco", "valor a vista", "a vista",
)
NEGATIONS = ("nao quero", "nao vou querer", "nao precisa", "sem interesse", "ja comprei", "desisti")


def _plain(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().split())


def detect(text: str) -> Optional[str]:
    """Frase de interesse em comprar (ou None)."""
    plain = _plain(text)
    if not plain or any(n in plain for n in NEGATIONS):
        return None
    for phrase in PHRASES:
        if re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", plain):
            return phrase
    return None


def on_inbound(lead: Lead, body: str, when: Optional[datetime.datetime] = None) -> bool:
    """Cliente escreveu depois de receber preço: tem cara de quem quer comprar? Marca o lead."""
    stage = lead.stage
    if lead.is_group or stage is None or stage.is_won or stage.is_lost or not lead.quoted_at:
        return False
    when = when or datetime.datetime.utcnow()
    if when < lead.quoted_at or not detect(body):
        return False
    lead.interest_at = when
    lead.interest_text = (body or "").strip().replace("\n", " ")[:255]
    return True


def pending(lead: Lead) -> bool:
    """O cliente quis comprar e o negócio ainda está em aberto: vale um follow-up."""
    stage = lead.stage
    return bool(lead.interest_at and stage is not None and not (stage.is_won or stage.is_lost))
