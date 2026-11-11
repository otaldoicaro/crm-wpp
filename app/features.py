"""Recursos que se liga/desliga por cliente (tenant.features), pra quando um ajuste é só de
um CRM ("[Só Conexão]"). Vazio = o padrão do ramo (DEFAULTS_BY_SEGMENT); a tela /prazos grava
a escolha do cliente ("-" = nenhum ligado). Pra um recurso novo: acrescente em FEATURES e use
features.has(tenant, "chave") no código."""

from app import segments

FEATURES = {
    "pagamento_parcelado": "Venda parcelada: forma de pagamento, entrada e parcelas no lead (lidas também da conversa, ex: “60x 2.473,25”, “entrada de 6.700”)",
    "visita_negociando": "Visita marcada na loja leva o lead pra Negociando",
    "interessados_negociando": "🔥 Aba “Interessados” do Inbox mostra os leads que estão na etapa Negociando (ou Agendamento)",
    "agendamentos": "📅 A etapa Negociando passa a se chamar Agendamento (visita marcada na loja). Preço ou parcela "
                    "enviados não mudam mais a etapa; só a visita combinada leva pra Agendamento",
    "lead_frio": "❄️ Lead frio: 7 dias sem o cliente responder, o lead vai pra etapa Lead frio (em vez de ir pra Perdido)",
    "sem_popup_outro_vendedor": "👥 Sem o aviso grande “Esse cliente também fala com…” ao abrir a conversa (fica só a faixa fina no topo)",
    "passar_troca_numero": "↪ Passar o lead pra outro vendedor já troca o WhatsApp de envio pro dele (sem a opção separada de "
                           "trocar o número)",
    "followup_automatico": "🤖 Follow-up automático: 24h sem resposta depois do preço → 1ª mensagem; mais 24h → 2ª; mais 6h → "
                           "Perdido · Não responde mais (só no horário de atendimento, textos abaixo)",
    "followup_so_24h": "🤖 Follow-up automático só de 24h: a gente chamou o cliente e ele não respondeu em 24h → 1 mensagem "
                       "(qualquer assunto). Sem 2ª mensagem e sem Perdido automático (no horário de atendimento, texto abaixo)",
    "mensagem_fora_horario": "🌙 Mensagem fora do horário: o cliente escreveu com a loja fechada → resposta automática do "
                             "WhatsApp do vendedor pedindo o que ele precisa e avisando que voltamos “amanhã cedo” (texto abaixo)",
    "funil_manual": "✋ Funil manual (venda consultiva): o CRM só passa Lead sem conversa → Novo → Em atendimento. "
                    "Qualificado, Ganho e Perdido pelas frases da conversa viram 💡 sugestão pro vendedor confirmar; "
                    "preço enviado não leva pra Negociando; comprovante vira “confirmar venda” (não fecha sozinho); "
                    "sem Perdido automático por 7 dias parado",
    "sem_passar_lead": "🚫 Sem passar lead pra outro vendedor: some a opção “Passar pra” da conversa, da ficha do lead e do "
                       "“Selecionar várias” (cada lead fica com quem atendeu)",
    "lixeira_apaga": "🗑️ Lixeira com prazo: lead excluído fica na Lixeira por 30 dias (dá pra restaurar) e depois é apagado de vez, "
                     "com a conversa",
    "sem_prazos": "⏰ Sem alertas de prazo (venda longa): some o “⏰ há X dias na etapa”, o “📞 follow-up · sem resposta” e o "
                  "“pode marcar Perdido” dos cards e das conversas, e esses leads saem dos filtros de atrasados",
    "tentativas_contato": "📞 1º, 2º e 3º contato: mostra no card do Pipeline (etapas antes de Qualificado) e no topo da "
                          "conversa quantas vezes o time chamou o cliente sem resposta (um por dia; o botão “+ tentativa” "
                          "conta ligação ou e-mail). Zera quando o cliente responde",
    "transcrever_audios": "🎙️ Transcrever os áudios do WhatsApp (o texto aparece embaixo do áudio e entra nas regras "
                          "automáticas e na busca). Custo no Groq: ~US$ 0,04 por hora de áudio",
}
# ⚙ Configurações → ⚙️ Recursos: os recursos agrupados por assunto (o que faltar aqui cai em "Outros")
GROUPS = [
    ("🧭 Funil e etapas", ["funil_manual", "visita_negociando", "agendamentos", "lead_frio", "sem_prazos", "tentativas_contato"]),
    ("💬 Conversas e Inbox", ["interessados_negociando", "sem_popup_outro_vendedor", "transcrever_audios"]),
    ("👥 Vendedores", ["sem_passar_lead", "passar_troca_numero"]),
    ("🤖 Mensagens automáticas pro cliente", ["followup_automatico", "followup_so_24h", "mensagem_fora_horario"]),
    ("💰 Venda e pagamento", ["pagamento_parcelado"]),
    ("🗑️ Lixeira", ["lixeira_apaga"]),
]


def grouped() -> list:
    """[(assunto, [chaves])] na ordem da tela, com o que não está em GROUPS no fim ("Outros")."""
    seen = {k for _, keys in GROUPS for k in keys}
    groups = [(name, [k for k in keys if k in FEATURES]) for name, keys in GROUPS]
    rest = [k for k in FEATURES if k not in seen]
    return [g for g in groups if g[1]] + ([("Outros", rest)] if rest else [])


DEFAULTS_BY_SEGMENT = {
    "seminovos": ("pagamento_parcelado", "visita_negociando"),
    "agencia": ("funil_manual", "interessados_negociando", "lixeira_apaga", "sem_prazos", "tentativas_contato"),
}


def enabled(tenant) -> set:
    raw = (getattr(tenant, "features", "") or "").strip()
    if not raw:
        return set(DEFAULTS_BY_SEGMENT.get(segments.key_for(tenant), ()))
    return {k for k in raw.split(",") if k in FEATURES}


def has(tenant, key: str) -> bool:
    return tenant is not None and key in enabled(tenant)


def store(keys) -> str:
    """Valor pra gravar em tenant.features a partir das chaves marcadas na tela."""
    chosen = [k for k in FEATURES if k in set(keys)]
    return ",".join(chosen) or "-"
