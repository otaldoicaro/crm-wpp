"""Enriquece a atribuição de leads de anúncio "Clique para WhatsApp" (CTWA)
com o NOME da campanha/conjunto/anúncio — o webhook da Cloud API só entrega
o `ad_id` (source_id), não os nomes legíveis.

Precisa de um token com escopo `ads_read` sobre a conta de anúncios do
cliente (gerado em Business Settings > Usuários do sistema > Gerar token,
selecionando a conta de anúncios e a permissão ads_read). É um token
DIFERENTE do META_CAPI_ACCESS_TOKEN (que só precisa de permissão de
Conversions API) — por isso é uma variável separada.

Sem essa variável configurada, a atribuição de CTWA continua funcionando
normalmente, só que sem nome de campanha (fica só o ad_id).
"""

import requests

from app.config import META_ADS_ACCESS_TOKEN, META_GRAPH_VERSION


def fetch_ad_names(ad_id: str) -> dict:
    """Retorna {"campaign_name": ..., "adset_name": ..., "ad_name": ...}.
    Em qualquer falha (token ausente, ad_id inválido, permissão faltando),
    retorna campos vazios — nunca levanta exceção, pra não travar o
    recebimento da mensagem no webhook."""
    empty = {"campaign_name": "", "adset_name": "", "ad_name": ""}
    if not META_ADS_ACCESS_TOKEN or not ad_id:
        return empty

    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{ad_id}"
    try:
        resp = requests.get(
            url,
            params={
                "fields": "name,adset{name},campaign{name}",
                "access_token": META_ADS_ACCESS_TOKEN,
            },
            timeout=8,
        )
        if not resp.ok:
            return empty
        data = resp.json()
    except requests.RequestException:
        return empty

    return {
        "campaign_name": data.get("campaign", {}).get("name", ""),
        "adset_name": data.get("adset", {}).get("name", ""),
        "ad_name": data.get("name", ""),
    }
