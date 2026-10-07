"""Gasto dos anúncios do Meta, por dia e por anúncio, direto da API de Marketing (o mesmo
token META_ADS_ACCESS_TOKEN que já preenche o nome da campanha dos leads).

- Contas de cada cliente: Tenant.meta_ad_accounts. Se estiver vazio, descobre sozinho
  perguntando ao Meta de qual conta são os anúncios que trouxeram leads pra esse cliente.
- 1ª vez: busca os últimos 90 dias. Depois, a cada 3h, os últimos 3 dias (o Meta ainda
  ajusta o gasto de ontem/anteontem).
- Grava em campaign_spend (source="meta_api"), trocando os dias buscados: rodar de novo
  nunca duplica.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading

import requests

from app.config import META_ADS_ACCESS_TOKEN, META_GRAPH_VERSION
from app.db import SessionLocal
from app.models import CampaignSpend, Lead, Tenant, UtmAttribution

logger = logging.getLogger("meta_spend")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_VERSION}"
FIRST_SYNC_DAYS = 90
REFRESH_DAYS = 3
INTERVAL_SECONDS = 3 * 60 * 60
INSIGHT_FIELDS = "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,impressions,clicks"


class MetaError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    if "access_token=" not in url:
        params["access_token"] = META_ADS_ACCESS_TOKEN
    resp = requests.get(url, params=params, timeout=60)
    data = resp.json() if resp.content else {}
    if not resp.ok:
        raise MetaError((data.get("error") or {}).get("message", resp.text[:300]))
    return data


def _clean_ids(text: str) -> list:
    return [p.strip().replace("act_", "") for p in (text or "").replace(";", ",").split(",") if p.strip()]


def detect_accounts(db, tenant: Tenant) -> list:
    """Contas de anúncio dos anúncios que trouxeram leads pra este cliente."""
    ad_ids = [
        ad_id
        for (ad_id,) in db.query(UtmAttribution.ad_id)
        .join(Lead, Lead.id == UtmAttribution.lead_id)
        .filter(Lead.tenant_id == tenant.id, UtmAttribution.ad_id != "")
        .distinct()
        .limit(20)
    ]
    accounts = []
    for ad_id in ad_ids:
        try:
            account = str(_get(f"{GRAPH}/{ad_id}", {"fields": "account_id"}).get("account_id", ""))
        except (MetaError, requests.RequestException):
            continue
        if account and account not in accounts:
            accounts.append(account)
    return accounts


def fetch_insights(account_id: str, since: datetime.date, until: datetime.date) -> list:
    """Uma linha por anúncio por dia (só dias com gasto ou impressão)."""
    url = f"{GRAPH}/act_{account_id}/insights"
    params = {
        "level": "ad",
        "fields": INSIGHT_FIELDS,
        "time_increment": 1,
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "limit": 500,
    }
    rows = []
    while url:
        data = _get(url, params)
        rows.extend(data.get("data", []))
        url = (data.get("paging") or {}).get("next")
        params = None  # o "next" já vem com todos os parâmetros
    return rows


def sync_tenant(db, tenant: Tenant, days: int | None = None) -> int:
    """Busca e grava o gasto do cliente. Devolve quantas linhas gravou."""
    accounts = _clean_ids(tenant.meta_ad_accounts)
    if not accounts:
        accounts = detect_accounts(db, tenant)
        if not accounts:
            return 0
        tenant.meta_ad_accounts = ",".join(accounts)
        db.commit()
        logger.info("meta_spend: %s -> contas de anúncio detectadas: %s", tenant.name, tenant.meta_ad_accounts)
    if days is None:
        has_data = db.query(CampaignSpend.id).filter(
            CampaignSpend.tenant_id == tenant.id, CampaignSpend.source == "meta_api"
        ).first()
        days = REFRESH_DAYS if has_data else FIRST_SYNC_DAYS
    until = datetime.date.today()
    since = until - datetime.timedelta(days=days - 1)

    rows = []
    for account in accounts:
        rows.extend(fetch_insights(account, since, until))
    # troca os dias buscados de uma vez (busca primeiro: se o Meta falhar, nada é apagado)
    db.query(CampaignSpend).filter(
        CampaignSpend.tenant_id == tenant.id,
        CampaignSpend.source == "meta_api",
        CampaignSpend.date >= since,
        CampaignSpend.date <= until,
    ).delete(synchronize_session=False)
    for r in rows:
        db.add(CampaignSpend(
            tenant_id=tenant.id,
            platform="meta",
            source="meta_api",
            date=datetime.date.fromisoformat(r["date_start"]),
            campaign=(r.get("campaign_name") or "")[:160],
            adset=(r.get("adset_name") or "")[:160],
            ad=(r.get("ad_name") or "")[:160],
            campaign_id=r.get("campaign_id", ""),
            adset_id=r.get("adset_id", ""),
            ad_id=r.get("ad_id", ""),
            spend=float(r.get("spend") or 0),
            impressions=int(r.get("impressions") or 0),
            clicks=int(r.get("clicks") or 0),
        ))
    db.commit()
    return len(rows)


def sync_all(days: int | None = None) -> dict:
    """Todos os clientes. Devolve {nome do cliente: linhas gravadas ou mensagem de erro}."""
    if not META_ADS_ACCESS_TOKEN:
        return {}
    db = SessionLocal()
    result = {}
    try:
        for tenant in db.query(Tenant).all():
            try:
                result[tenant.name] = sync_tenant(db, tenant, days)
            except (MetaError, requests.RequestException) as exc:
                db.rollback()
                result[tenant.name] = f"erro: {exc}"
                logger.warning("meta_spend: %s falhou: %s", tenant.name, exc)
        return result
    finally:
        db.close()


def schedule(delay_seconds: int = 90) -> None:
    if not META_ADS_ACCESS_TOKEN:
        return

    def run():
        try:
            result = sync_all()
            if result:
                logger.info("meta_spend: gasto atualizado %s", result)
        except Exception:
            logger.exception("meta_spend: falhou")
        schedule(INTERVAL_SECONDS)

    timer = threading.Timer(delay_seconds, run)
    timer.daemon = True
    timer.start()
