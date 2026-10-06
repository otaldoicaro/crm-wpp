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

import time

import requests

from app.config import META_ADS_ACCESS_TOKEN, META_GRAPH_VERSION

# muitos leads vêm do mesmo anúncio: guarda os nomes por 6h pra não consultar o Meta toda vez
_CACHE: dict = {}
_CACHE_TTL = 6 * 60 * 60


def fetch_ad_names(ad_id: str) -> dict:
    """Retorna {"campaign_name": ..., "adset_name": ..., "ad_name": ...}.
    Em qualquer falha (token ausente, ad_id inválido, permissão faltando),
    retorna campos vazios — nunca levanta exceção, pra não travar o
    recebimento da mensagem no webhook."""
    empty = {"campaign_name": "", "adset_name": "", "ad_name": ""}
    if not META_ADS_ACCESS_TOKEN or not ad_id:
        return empty
    cached = _CACHE.get(ad_id)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

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

    names = {
        "campaign_name": data.get("campaign", {}).get("name", ""),
        "adset_name": data.get("adset", {}).get("name", ""),
        "ad_name": data.get("name", ""),
    }
    _CACHE[ad_id] = (time.time(), names)
    return names


def backfill_campaign_names() -> tuple:
    """Preenche campanha/conjunto/anúncio dos leads de anúncio que chegaram antes do
    token existir. Devolve (atualizados, sem_acesso)."""
    from app.db import SessionLocal
    from app.models import UtmAttribution

    db = SessionLocal()
    try:
        updated = missing = 0
        rows = db.query(UtmAttribution).filter(UtmAttribution.ad_id != "", UtmAttribution.utm_campaign == "").all()
        for row in rows:
            names = fetch_ad_names(row.ad_id)
            if not names["campaign_name"]:
                missing += 1
                continue
            row.utm_campaign, row.utm_term, row.utm_content = names["campaign_name"], names["adset_name"], names["ad_name"]
            db.add(row)
            updated += 1
        db.commit()
        return updated, missing
    finally:
        db.close()


def check_token() -> list:
    """Contas de anúncio que o token enxerga: [(nome, act_id)]. Levanta erro se o token não vale."""
    resp = requests.get(
        f"https://graph.facebook.com/{META_GRAPH_VERSION}/me/adaccounts",
        params={"fields": "name,account_id", "limit": 100, "access_token": META_ADS_ACCESS_TOKEN},
        timeout=15,
    )
    data = resp.json()
    if not resp.ok:
        raise RuntimeError((data.get("error") or {}).get("message", resp.text))
    return [(a.get("name", ""), a.get("account_id", "")) for a in data.get("data", [])]
