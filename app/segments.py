"""O que muda conforme o ramo do cliente (tenant.segment): motivos de perda, exemplos nos
textos e o tipo de venda. As frases das sugestões de etapa ficam em services/suggestions.py
(RULES_BY_SEGMENT). Pra um ramo novo: copie um bloco, ajuste e crie as regras de lá também.
Cliente sem ramo (ou com um ramo que não existe mais) usa "autopecas"."""

NO_REPLY_REASON = "Não responde mais"
INVALID_REASON = "Contato inválido"  # o WhatsApp diz que o número não existe (testado com e sem o 9)
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
            INVALID_REASON,
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
            INVALID_REASON,
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
        # 🤖 follow-up automático (24h sem resposta): vazio = autofollow.DEFAULT_TEXT1 (fala de peça)
        "autofu_text1": "Oi! Passando pra saber se você ainda tem interesse no carro que conversamos. 🚗\n\n"
                        "🙂 Se tiver qualquer dúvida ou quiser agendar uma visita, me chama aqui que dou continuidade no atendimento.",
        # 📅 agendamentos ({nome}, {data}, {hora}, {carro}, {empresa}, {vendedor}); linha com dado vazio sai
        # 🌙 mensagem fora do horário: vazio = after_hours.DEFAULT_TEXT (fala de peça)
        "after_hours_text": "Olá, {nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. 😊\n\n"
                            "No momento estamos fora do horário de atendimento ({horario}).\n\n"
                            "Já me conta aqui qual carro você procura e se tem carro pra dar na troca, que {retorno}, "
                            "assim que eu voltar, já te ajudo.\n\n"
                            "Só aguardar que te respondo! 🙏",
        "after_hours_detail_text": "",  # sem a versão "já disse a peça, o carro e o ano"
        "agenda_confirm_text": "{nome}, sua visita está confirmada! 🚗\n📅 {data} às {hora}\n🚘 {carro}\n"
                               "Qualquer imprevisto, é só me avisar por aqui 😉",
        "agenda_reminder_text": "Oi {nome}! Passando pra lembrar da sua visita hoje às {hora} pra ver o {carro}. Te espero aqui! 😉",
        "auto_hello_stage": "Em atendimento",  # formulário de carro não diz se vai comprar: o time já falou
    },
    # agência de marketing (vende serviço: tráfego, CRM, LPs…); os leads são empresas
    "agencia": {
        "label": "Agência de marketing",
        "loss_reasons": [
            "Preço",
            "Sem verba no momento",
            "Vai fazer interno",
            "Fechou com outra agência",
            "Timing / não é o momento",
            "Fora do perfil (faturamento abaixo do mínimo)",
            "Desistiu / só pesquisando",
            REFUND_REASON,
            NO_REPLY_REASON,
            INVALID_REASON,
            "Outro",
        ],
        "loss_placeholder": "Ex: gostou da proposta, mas só tem verba a partir de março",
        "sale_kind": "contratação de serviços de marketing (mensalidade ou projeto)",
        "receipt_is_sale": False,  # a venda é o contrato assinado: comprovante de pagamento não vira venda
        "form_summary": "ver empresa, faturamento e verba",
        "first_message": "Olá{nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. Vi que você pediu o diagnóstico "
                         "gratuito! Me conta rapidinho como a sua empresa vende hoje e onde quer crescer, "
                         "que eu já marco uma conversa com você 😉",
        "auto_hello_text": "Olá, {nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. Vi que você pediu o diagnóstico "
                           "gratuito! Me conta rapidinho como a sua empresa vende hoje e onde quer crescer, "
                           "que eu já marco uma conversa com você 😉",
        "auto_hello_stage": "Em atendimento",  # o formulário não diz se vai contratar: o comercial qualifica
        # etapas do funil de quem é criado nesse ramo (venda consultiva, com reuniões): qualquer mensagem
        # tira de "Lead sem conversa" pra "Em atendimento"; dali em diante quem move é o vendedor.
        # "Proposta enviada" é a etapa de oportunidade (funnel.NEGOTIATION_NAMES) e a Venda conta no
        # contrato assinado
        "stages": [
            ("Lead sem conversa", "", False, False),
            ("Em atendimento", "", False, False),
            ("Qualificado", "Qualified", False, False),
            ("R2 Agendada", "", False, False),
            ("Proposta enviada", "", False, False),
            ("Venda", "Purchase", True, False),
            ("Perdido", "", False, True),
        ],
        # clientes criados com o funil antigo: no start, etapa renomeada / tirada (os leads dela vão pra outra)
        "stage_renames": {"Lead": "Em atendimento"},
        "stage_removed": {"Triagem (R1)": "Em atendimento"},
        # venda consultiva: Pipeline só com o filtro ⭐ Favoritas; Inbox sem "Venda a confirmar" e sem
        # "Follow-up interessado" (preço/comprovante), e a aba 🔥 vira 📄 Propostas (etapa Proposta enviada)
        "consultive": True,
        # textos dos recursos automáticos (todos desligados por padrão; se ligar, não falam de peça)
        "autofu_text1": "Oi! Passando pra saber se ainda faz sentido a gente conversar sobre o crescimento da sua empresa. 🚀\n\n"
                        "🙂 Se quiser marcar o diagnóstico ou tiver alguma dúvida, me chama aqui.",
        "after_hours_text": "Olá, {nome}! Tudo bem? Aqui é {vendedor}, da {empresa}. 😊\n\n"
                            "No momento estamos fora do horário de atendimento ({horario}).\n\n"
                            "Já me conta aqui o que a sua empresa vende e onde quer crescer, que {retorno}, "
                            "assim que eu voltar, já te respondo.\n\n"
                            "Só aguardar! 🙏",
        "after_hours_detail_text": "",
        "agenda_confirm_text": "{nome}, nossa reunião está confirmada! 📅 {data} às {hora}\n"
                               "Qualquer imprevisto, é só me avisar por aqui 😉",
        "agenda_reminder_text": "Oi {nome}! Passando pra lembrar da nossa reunião hoje às {hora}. Até já! 😉",
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
