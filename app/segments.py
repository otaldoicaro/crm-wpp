"""O que muda conforme o ramo do cliente (tenant.segment): motivos de perda, exemplos nos
textos e o tipo de venda. As frases das sugestões de etapa ficam em services/suggestions.py
(RULES_BY_SEGMENT). Pra um ramo novo: copie um bloco, ajuste e crie as regras de lá também.
Cliente sem ramo (ou com um ramo que não existe mais) usa "autopecas"."""

NO_REPLY_REASON = "Não responde mais"
REFUND_REASON = "Devolução / reembolso"  # comprou e pediu o dinheiro de volta

SEGMENTS = {
    "autopecas": {
        "label": "Autopeças",
        "loss_reasons": [
            "Não temos a peça",
            "Preço",
            "Comprou em outro lugar / concorrente",
            "Prazo de entrega / disponibilidade",
            "Frete",
            "Forma de pagamento / condições",
            "Não informou o veículo",
            "Desistiu / só pesquisando",
            "Resolveu de outro jeito",
            REFUND_REASON,
            NO_REPLY_REASON,
            "Outro",
        ],
        "loss_placeholder": "Ex: queria o farol original, só temos paralelo",
        "sale_kind": "venda de peças",  # vai no texto da IA de comprovantes
        "form_summary": "ver carro e peças",  # inbox: resumo do formulário da LP
        # 1ª mensagem pra lead que chegou e nunca mandou mensagem ({nome}, {vendedor}, {empresa})
        "first_message": "Olá{nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. Vi que você se interessou "
                         "pelas nossas peças! Me passa qual peça você procura, o modelo do carro e o ano, "
                         "que eu já verifico pra você 😉",
        # 🤖 mensagem automática pro lead do formulário (first_contact): vazio = first_contact.DEFAULT_TEXT;
        # e pra qual etapa o lead vai depois dela (o cliente da LP já disse carro, ano e peça)
        "auto_hello_text": "",
        "auto_hello_stage": "Qualificado",
    },
    "seminovos": {
        "label": "Seminovos",
        "loss_reasons": [
            "Preço",
            "Comprou em outro lugar",
            "Não temos o carro / modelo",
            "Financiamento não aprovado",
            "Avaliação da troca não agradou",
            "Desistiu / só pesquisando",
            REFUND_REASON,
            NO_REPLY_REASON,
            "Outro",
        ],
        "loss_placeholder": "Ex: queria o mesmo modelo com câmbio automático",
        "sale_kind": "venda de carros seminovos",
        "form_summary": "ver o que ele preencheu",
        "first_message": "Olá{nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. Vi que você se interessou "
                         "pelos nossos carros! Me conta qual modelo você procura e se tem carro pra dar na troca, "
                         "que eu já te ajudo 😉",
        # aprovado pelo Icaro em 21/10/2026 (mesma pegada da 1ª mensagem manual)
        "auto_hello_text": "Olá, {nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. Vi que você se interessou "
                           "pelos nossos carros! Me conta qual modelo você procura e se tem carro pra dar na troca, "
                           "que eu já te ajudo 😉",
        "auto_hello_stage": "Em atendimento",  # formulário de carro não diz se vai comprar: o time já falou
    },
}
DEFAULT_SEGMENT = "autopecas"


def key_for(tenant) -> str:
    key = getattr(tenant, "segment", "") or DEFAULT_SEGMENT
    return key if key in SEGMENTS else DEFAULT_SEGMENT


def segment_for(tenant) -> dict:
    return SEGMENTS[key_for(tenant)]


def loss_reasons(tenant) -> list:
    return segment_for(tenant)["loss_reasons"]


def key_for_lead(lead) -> str:
    """Ramo do cliente do lead, mesmo com o lead recém-criado (ainda sem lead.tenant carregado)."""
    tenant = lead.tenant
    if tenant is None and lead.tenant_id:
        from sqlalchemy.orm import object_session

        from app.models import Tenant

        session = object_session(lead)
        tenant = session.get(Tenant, lead.tenant_id) if session else None
    return key_for(tenant)
