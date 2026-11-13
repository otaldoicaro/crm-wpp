"""📊 Gasto do Google Ads no CRM, por um script que roda dentro da conta do Google Ads do cliente.

Sem API oficial (que pede "token de desenvolvedor" aprovado pelo Google): o cliente cola o script
(script_for) em Google Ads → Ferramentas → Ações em massa → Scripts e programa pra rodar todo dia.
O script manda o gasto, as impressões e os cliques de cada campanha dos últimos 30 dias pra
/webhooks/google-ads/<id do cliente>?chave=<key_for>; o CRM troca esses dias (rodar de novo nunca
duplica). O gasto cai em campaign_spend (platform="google") e aparece no 📊 Tráfego, casando com os
leads pelo nome da campanha (utm_campaign).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import logging

from app.config import SECRET_KEY

logger = logging.getLogger("google_ads_spend")

SOURCE = "google_ads_script"
MAX_ROWS = 20000


def key_for(tenant_id: str) -> str:
    """Chave do endereço de cada cliente (tirada do SECRET_KEY do servidor, fora do código)."""
    return hmac.new(SECRET_KEY.encode(), f"google-ads|{tenant_id}".encode(), hashlib.sha256).hexdigest()[:32]


def valid_key(tenant_id: str, key: str) -> bool:
    return bool(key) and hmac.compare_digest(key_for(tenant_id), key)


def _num(value, kind=float):
    try:
        return kind(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return kind(0)


def save(db, tenant_id: str, payload: dict) -> int:
    """Grava as linhas do script ({"rows": [{date, campaign, spend, impressions, clicks}]}). Troca
    os dias que vieram (do mais antigo em diante) pelas linhas novas. Devolve quantas gravou."""
    from app.models import CampaignSpend

    rows = []
    for item in (payload.get("rows") or [])[:MAX_ROWS]:
        if not isinstance(item, dict):
            continue
        try:
            day = datetime.date.fromisoformat(str(item.get("date", ""))[:10])
        except ValueError:
            continue
        name = str(item.get("campaign") or "").strip()[:160]
        if not name:
            continue
        rows.append((day, name, str(item.get("adset") or "").strip()[:160], round(_num(item.get("spend")), 2),
                     _num(item.get("impressions"), int), _num(item.get("clicks"), int)))
    start = payload.get("from")
    try:
        start = datetime.date.fromisoformat(str(start)[:10]) if start else None
    except ValueError:
        start = None
    if rows:
        start = min([start] + [r[0] for r in rows]) if start else min(r[0] for r in rows)
    if start is None:
        return 0
    _save_clicks(db, tenant_id, payload.get("clicks") or [])
    db.query(CampaignSpend).filter(CampaignSpend.tenant_id == tenant_id, CampaignSpend.platform == "google",
                                   CampaignSpend.source == SOURCE, CampaignSpend.date >= start).delete(synchronize_session=False)
    for day, name, adset, spend, impressions, clicks in rows:
        db.add(CampaignSpend(tenant_id=tenant_id, platform="google", campaign=name, adset=adset, date=day, spend=spend,
                             impressions=impressions, clicks=clicks, source=SOURCE))
    db.commit()
    logger.info("google ads: %s linhas de gasto gravadas pro cliente %s (desde %s, conta %s)",
                len(rows), tenant_id, start, payload.get("account", ""))
    return len(rows)


def _save_clicks(db, tenant_id: str, clicks: list) -> None:
    """Cliques (gclid → campanha/grupo) dos dias que vieram: troca esses dias, sem duplicar."""
    from app.models import GoogleClick

    rows = []
    for item in clicks[:MAX_ROWS * 5]:
        if not isinstance(item, dict) or not item.get("gclid"):
            continue
        try:
            day = datetime.date.fromisoformat(str(item.get("date", ""))[:10])
        except ValueError:
            continue
        rows.append((str(item["gclid"])[:255], str(item.get("campaign") or "")[:160], str(item.get("adset") or "")[:160], day))
    if not rows:
        return
    days = {r[3] for r in rows}
    db.query(GoogleClick).filter(GoogleClick.tenant_id == tenant_id, GoogleClick.date.in_(days)).delete(synchronize_session=False)
    for gclid, campaign, adset, day in rows:
        db.add(GoogleClick(tenant_id=tenant_id, gclid=gclid, campaign=campaign, adset=adset, date=day))
    logger.info("google ads: %s cliques (gclid) gravados pro cliente %s", len(rows), tenant_id)


def clicks_by_gclid(db, tenant_id: str) -> dict:
    """{gclid: (campanha, grupo)} de todos os cliques conhecidos do cliente."""
    from app.models import GoogleClick

    return {g: (c, a) for g, c, a in db.query(GoogleClick.gclid, GoogleClick.campaign, GoogleClick.adset)
            .filter(GoogleClick.tenant_id == tenant_id)}


def last_sync(db, tenant_id: str):
    """(último dia com gasto, total de linhas) pra mostrar no card."""
    from sqlalchemy import func

    from app.models import CampaignSpend

    return db.query(func.max(CampaignSpend.date), func.count(CampaignSpend.id)).filter(
        CampaignSpend.tenant_id == tenant_id, CampaignSpend.source == SOURCE).one()


SCRIPT = """// CRM da Agência Junta: manda o gasto das campanhas do Google Ads pro CRM (versão 3: campanha, grupo de anúncios e cliques).
// Rode 1x (Visualizar/Executar) e programe pra rodar Diariamente.
var CRM_URL = "%(url)s";
var DIAS = 30;

function main() {
  var hoje = new Date();
  var inicio = new Date(hoje.getTime() - (DIAS - 1) * 24 * 60 * 60 * 1000);
  var tz = AdsApp.currentAccount().getTimeZone();
  var de = Utilities.formatDate(inicio, tz, "yyyy-MM-dd");
  var ate = Utilities.formatDate(hoje, tz, "yyyy-MM-dd");
  var consulta = "SELECT segments.date, campaign.name, ad_group.name, metrics.cost_micros, metrics.impressions, metrics.clicks " +
                 "FROM ad_group WHERE segments.date BETWEEN '" + de + "' AND '" + ate + "' AND metrics.impressions > 0";
  var linhas = [];
  var it = AdsApp.search(consulta);
  while (it.hasNext()) {
    var r = it.next();
    linhas.push({
      date: r.segments.date,
      campaign: r.campaign.name,
      adset: r.adGroup ? r.adGroup.name : "",
      spend: Number(r.metrics.costMicros || 0) / 1000000,
      impressions: Number(r.metrics.impressions || 0),
      clicks: Number(r.metrics.clicks || 0)
    });
  }
  // cliques (gclid) de cada dia: liga o lead à campanha mesmo com a UTM escrita à mão
  var cliques = [];
  for (var d = 0; d < DIAS; d++) {
    var dia = Utilities.formatDate(new Date(hoje.getTime() - d * 24 * 60 * 60 * 1000), tz, "yyyy-MM-dd");
    try {
      var c = AdsApp.search("SELECT click_view.gclid, campaign.name, ad_group.name, segments.date FROM click_view " +
                            "WHERE segments.date = '" + dia + "'");
      while (c.hasNext()) {
        var k = c.next();
        cliques.push({ gclid: k.clickView.gclid, campaign: k.campaign.name, adset: k.adGroup ? k.adGroup.name : "", date: dia });
      }
    } catch (e) { Logger.log("cliques de " + dia + ": " + e); }
  }
  var resposta = UrlFetchApp.fetch(CRM_URL, {
    method: "post",
    contentType: "application/json",
    payload: JSON.stringify({ account: AdsApp.currentAccount().getCustomerId(), from: de, rows: linhas, clicks: cliques }),
    muteHttpExceptions: true
  });
  Logger.log("CRM respondeu " + resposta.getResponseCode() + ": " + resposta.getContentText());
}
"""


def endpoint(tenant) -> str:
    from app.tenancy import public_url

    return f"{public_url(tenant)}/webhooks/google-ads/{tenant.id}?chave={key_for(tenant.id)}"


def script_for(tenant) -> str:
    return SCRIPT % {"url": endpoint(tenant)}
