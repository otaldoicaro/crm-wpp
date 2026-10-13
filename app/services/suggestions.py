"""Sugestões de etapa lidas nas mensagens. O CRM nunca muda a etapa sozinho com isto: mostra
no topo da conversa "💡 parece que ... — confirmar?" e o vendedor confirma ou descarta.

- Qualificado: o cliente passou os dados do carro (placa, modelo + ano, chassi), ou o time
  respondeu que tem a peça ("temos sim", "tenho em estoque"...).
- Ganho sem comprovante: o cliente vai pagar na hora/na retirada, ou o time fala em pedido
  confirmado, nota fiscal, rastreio, saiu pra entrega...
- Perdido (com o motivo já escolhido): "já comprei", "achei mais barato", "desisti"... ou o
  time disse que não tem a peça.
- Outro (não é venda): fornecedor, transportadora, currículo, cobrança...

As frases acima são do ramo autopeças; cada ramo (tenant.segment) tem as suas em
RULES_BY_SEGMENT. Seminovos: troca, visita, test drive, entrada/financiamento (qualificado),
contrato/sinal (ganho), crédito negado, avaliação da troca, carro já vendido (perdido).

Tudo grátis: regras de texto, sem IA. Cada tipo descartado não volta a ser sugerido no mesmo
negócio."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from app import segments
from app.models import Lead

KIND_LABEL = {
    "qualificado": "Parece qualificado",
    "ganho": "Parece que a venda fechou",
    "perdido": "Parece que o lead foi perdido",
    "outro": "Parece que não é venda",
}
PRIORITY = {"ganho": 4, "perdido": 3, "qualificado": 2, "outro": 1}

CAR_MODELS = (
    "gol|palio|uno|onix|hb20|corsa|celta|civic|corolla|fiesta|ka|fox|polo|saveiro|strada|siena|sandero|logan|"
    "hilux|s10|ranger|toro|kicks|creta|compass|renegade|tracker|cruze|prisma|voyage|golf|jetta|fit|city|hr-v|hrv|"
    "march|versa|sentra|spin|cobalt|montana|idea|punto|argo|mobi|cronos|duster|clio|megane|208|207|206|2008|3008|"
    "c3|c4|ecosport|focus|fusion|amarok|frontier|l200|outlander|tucson|ix35|i30|azera|sportage|cerato|picanto|"
    "etios|yaris|crossfox|spacefox|parati|kombi|astra|vectra|zafira|meriva|agile|classic|doblo|fiorino|weekend|"
    "tiguan|t-cross|nivus|virtus|up|kwid|oroch|captur|fluence|symbol|hb20s|sw4|rav4|camry|accord|wr-v|biz|cg|titan|fan"
)
_PLATE = re.compile(r"\b[a-z]{3}-?\d[a-z0-9]\d{2}\b")
_YEAR = re.compile(r"\b(19[89]\d|20[0-3]\d)\b|\bano\s+\d{2}\b")
_MODEL = re.compile(rf"\b({CAR_MODELS})\b")


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower().split())


def _any(text: str, phrases: tuple) -> Optional[str]:
    for p in phrases:
        if re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", text):
            return p
    return None


# (tipo, quem escreve: "in" cliente / "out" time, frases, motivo de perda)
RULES_AUTOPECAS = [
    ("ganho", "in", ("pago na hora", "pagar na hora", "pagamento na hora", "pago na retirada", "pago quando buscar",
                     "pago quando chegar", "pago na entrega", "pagar na entrega", "pago no balcao", "vou buscar",
                     "passo ai pra buscar", "passo ai pegar", "ja estou indo", "to indo buscar", "pode separar",
                     "fechado entao", "pode fechar", "vou levar"), ""),
    ("ganho", "out", ("pedido confirmado", "venda confirmada", "pagamento confirmado", "pagamento recebido",
                      "recebemos o pagamento", "nota fiscal", "codigo de rastreio", "codigo de rastreamento",
                      "saiu para entrega", "saiu pra entrega", "ja esta separado", "ja separei", "separado pra voce",
                      "pode vir buscar", "pode vir retirar", "obrigado pela compra", "obrigado pela preferencia"), ""),
    ("perdido", "in", ("ja comprei", "comprei em outro", "comprei em outra", "consegui em outro", "consegui em outra",
                       "ja consegui", "achei em outro", "ja achei", "ja resolvi"), "Comprou em outro lugar / concorrente"),
    ("perdido", "in", ("achei mais barato", "achei mais em conta", "muito caro", "ta caro", "esta caro", "caro demais",
                       "fora do meu orcamento"), "Preço"),
    ("perdido", "in", ("nao preciso mais", "nao precisa mais", "desisti", "deixa pra la", "vou deixar pra depois",
                       "so pesquisando", "so cotando", "peguei o contato", "pegar o contato", "peguei seu contato", "salvei o contato", "salvei seu contato",
                       "vou salvar o contato", "vou salvar seu contato", "quando precisar", "quando eu precisar",
                       "fica pra proxima", "fica para proxima", "fica pra outra", "outra hora", "por enquanto nao",
                       "no momento nao", "vou ver e te aviso", "qualquer coisa eu chamo", "qualquer coisa te chamo",
                       "qualquer coisa eu volto", "qualquer coisa volto"), "Desistiu / só pesquisando"),
    ("perdido", "in", ("demora muito", "prazo muito longo", "preciso pra hoje", "nao da pra esperar"),
       "Prazo de entrega / disponibilidade"),
    ("perdido", "out", ("nao temos", "nao tenho essa", "nao tenho esse", "nao trabalhamos", "em falta",
                        "sem estoque", "nao temos em estoque", "infelizmente nao temos", "nao encontrei essa",
                        "nao encontramos", "nao possuimos", "nao possuo", "nao temos mais", "nao tem mais",
                        "infelizmente nao", "ja foi vendido", "ja foi vendida", "ja foram vendidos", "foi vendido",
                        "ja vendemos", "ja vendi", "esgotado", "esgotada", "acabou o estoque", "indisponivel"),
       "Não temos a peça"),
    ("qualificado", "out", ("temos sim", "tem sim", "tenho sim", "sim temos", "sim tenho", "temos em estoque",
                            "tenho em estoque", "temos disponivel", "tenho disponivel", "temos a peca", "tenho a peca",
                            "tenho aqui", "temos aqui", "disponivel sim", "tem disponivel"), ""),
    ("outro", "in", ("sou da transportadora", "curriculo", "vaga de emprego", "trabalhar com voces",
                     "sou fornecedor", "somos fornecedores", "represento a empresa", "sou representante", "sou entregador", "cobranca referente", "boleto em aberto", "parceria comercial"), ""),
]


OTHER_PHRASES = RULES_AUTOPECAS[-1][2]  # "não é venda" vale pra qualquer ramo

RULES_SEMINOVOS = [
    ("ganho", "in", ("fechado entao", "pode fechar", "quero fechar", "vou fechar", "negocio fechado", "vou levar",
                     "vou ficar com ele", "vou ficar com o carro", "pode reservar pra mim", "pode separar pra mim"), ""),
    ("ganho", "out", ("contrato assinado", "venda confirmada", "pagamento confirmado", "pagamento recebido",
                      "recebemos o pagamento", "sinal recebido", "recebemos o sinal", "parabens pelo carro",
                      "parabens pela compra", "parabens pela aquisicao", "obrigado pela compra",
                      "obrigado pela preferencia", "transferencia do veiculo", "carro ja esta no seu nome"), ""),
    ("perdido", "in", ("ja comprei", "comprei outro", "comprei em outro", "comprei em outra", "fechei com outra",
                       "fechei com outro", "fechei em outra", "fechei em outro", "achei outro carro", "ja achei",
                       "ja resolvi"), "Comprou em outro lugar"),
    ("perdido", "in", ("achei mais barato", "achei mais em conta", "muito caro", "ta caro", "esta caro", "caro demais",
                       "fora do meu orcamento", "acima da fipe", "acima da tabela"), "Preço"),
    ("perdido", "in", ("nao preciso mais", "desisti", "deixa pra la", "vou deixar pra depois", "so pesquisando",
                       "so olhando", "so cotando", "mais pra frente", "nao e o momento", "peguei o contato", "pegar o contato", "peguei seu contato", "salvei o contato", "salvei seu contato",
                       "vou salvar o contato", "vou salvar seu contato", "quando precisar", "quando eu precisar",
                       "fica pra proxima", "fica para proxima", "fica pra outra", "outra hora", "por enquanto nao",
                       "no momento nao", "vou ver e te aviso", "qualquer coisa eu chamo", "qualquer coisa te chamo",
                       "qualquer coisa eu volto", "qualquer coisa volto"),
       "Desistiu / só pesquisando"),
    ("perdido", "in", ("financiamento nao aprovado", "financiamento negado", "credito negado", "nao aprovou",
                       "nao foi aprovado", "reprovado", "estou negativado", "to negativado", "nome sujo"),
       "Financiamento não aprovado"),
    ("perdido", "out", ("financiamento nao foi aprovado", "credito nao foi aprovado", "nao aprovaram",
                        "financiamento negado", "credito negado", "ficha reprovada", "reprovado"),
       "Financiamento não aprovado"),
    ("perdido", "in", ("avaliacao muito baixa", "pouco pelo meu carro", "pagaram mais no meu", "meu carro vale mais",
                       "nao aceito esse valor na troca"), "Avaliação da troca não agradou"),
    ("perdido", "out", ("ja foi vendido", "ja vendemos", "foi vendido", "nao temos mais", "nao temos esse",
                        "nao temos nenhum", "infelizmente nao temos", "nao temos no estoque", "sem estoque",
                        "nao possuimos", "infelizmente nao"),
       "Não temos o carro / modelo"),
    ("qualificado", "in", ("aceita troca", "aceitam troca", "pega troca", "pegam troca", "carro na troca",
                           "quero agendar", "posso ir ver", "posso passar ai", "vou passar ai", "quero ver o carro",
                           "ver pessoalmente", "test drive", "valor da entrada", "quanto de entrada", "qual a entrada",
                           "simulacao", "simular", "faz financiamento", "fazem financiamento", "quero financiar",
                           "consigo financiar", "pagamento a vista"), ""),
    ("qualificado", "out", ("visita agendada", "esta agendado", "fica agendado", "te espero", "pode vir ver",
                            "financiamento aprovado", "credito aprovado", "ficha aprovada"), ""),
    ("outro", "in", OTHER_PHRASES, ""),
]

# por ramo: frases e se dados do carro (placa, modelo + ano, chassi) contam como qualificado
RULES_BY_SEGMENT = {"autopecas": (RULES_AUTOPECAS, True), "seminovos": (RULES_SEMINOVOS, False)}
RULES = RULES_AUTOPECAS  # nome antigo


def detect(text: str, outbound: bool, segment: str = segments.DEFAULT_SEGMENT) -> Optional[tuple]:
    """(tipo, frase achada, motivo de perda) da regra mais forte que bate, ou None."""
    plain = _norm(text)
    if not plain:
        return None
    rules, vehicle_data = RULES_BY_SEGMENT.get(segment, RULES_BY_SEGMENT[segments.DEFAULT_SEGMENT])
    who = "out" if outbound else "in"
    best = None
    for kind, side, phrases, reason in rules:
        if side != who:
            continue
        found = _any(plain, phrases)
        if found and (best is None or PRIORITY[kind] > PRIORITY[best[0]]):
            best = (kind, found, reason)
    if best is None and not outbound and vehicle_data:  # dados do carro = cliente qualificado
        plate = _PLATE.search(plain)
        model, year = _MODEL.search(plain), _YEAR.search(plain)
        if plate or "chassi" in plain or (model and year):
            best = ("qualificado", "dados do veículo", "")
    return best


def _applies(lead: Lead, kind: str) -> bool:
    from app.services import funnel

    stage = lead.stage
    if lead.is_group or stage is None or kind in (lead.suggest_dismissed or "").split(","):
        return False
    if kind == "outro":
        return lead.tag != "outro"
    if stage.is_won or stage.is_lost:
        return False
    if kind == "qualificado":
        return stage.order < funnel.levels(funnel._stages_cached(lead))["qualified"]
    return True


def on_message(lead: Lead, body: str, outbound: bool) -> None:
    """Chamado a cada mensagem com texto. Só troca a sugestão atual por uma mais forte."""
    hit = detect(body, outbound, segments.key_for_lead(lead))
    if hit is None:
        return
    kind, phrase, reason = hit
    if not _applies(lead, kind):
        return
    if lead.suggest_kind and PRIORITY.get(lead.suggest_kind, 0) > PRIORITY[kind] and _applies(lead, lead.suggest_kind):
        return
    excerpt = (body or "").strip().replace("\n", " ")
    lead.suggest_kind, lead.suggest_loss_reason = kind, reason
    lead.suggest_text = (f"“{excerpt[:180]}”" if phrase != "dados do veículo" else f"dados do veículo: “{excerpt[:160]}”")[:255]


def current(lead: Lead) -> Optional[str]:
    """Sugestão pendente ainda válida (o lead pode ter mudado de etapa depois)."""
    if lead.suggest_kind and _applies(lead, lead.suggest_kind):
        return lead.suggest_kind
    return None


def dismiss(lead: Lead) -> None:
    kinds = [k for k in (lead.suggest_dismissed or "").split(",") if k]
    if lead.suggest_kind and lead.suggest_kind not in kinds:
        kinds.append(lead.suggest_kind)
    lead.suggest_dismissed = ",".join(kinds)[:60]
    lead.suggest_kind = ""
