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
    "transcrever_audios": "🎙️ Transcrever os áudios do WhatsApp (o texto aparece embaixo do áudio e entra nas regras "
                          "automáticas e na busca). Custo no Groq: ~US$ 0,04 por hora de áudio",
}
DEFAULTS_BY_SEGMENT = {
    "seminovos": ("pagamento_parcelado", "visita_negociando"),
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
