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
from app.models import AdCreative, CampaignSpend, Lead, Tenant, UtmAttribution

logger = logging.getLogger("meta_spend")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_VERSION}"
FIRST_SYNC_DAYS = 90
REFRESH_DAYS = 3
INTERVAL_SECONDS = 3 * 60 * 60
INSIGHT_FIELDS = (
    "campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,spend,impressions,clicks,reach,"
    "inline_link_clicks,actions"
)


def _action(row: dict, *types: str) -> int:
    total = 0
    for item in row.get("actions") or []:
        if item.get("action_type") in types:
            try:
                total += int(float(item.get("value") or 0))
            except ValueError:
                pass
    return total


def _results(row: dict) -> int:
    """"Resultado" do Meta: cadastros (o "lead" já soma pixel + formulário; sem ele, soma os dois)
    + conversas iniciadas no WhatsApp/Direct."""
    leads = _action(row, "lead") or _action(row, "offsite_conversion.fb_pixel_lead", "onsite_conversion.lead_grouped")
    return leads + _action(row, "onsite_conversion.messaging_conversation_started_7d")


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


CHUNK_DAYS = 10  # 90 dias de uma vez, por anúncio e por dia, passa do limite do Meta ("reduce the amount of data")


def fetch_insights(account_id: str, since: datetime.date, until: datetime.date) -> list:
    """Uma linha por anúncio por dia, buscando de 10 em 10 dias."""
    rows, start = [], since
    while start <= until:
        end = min(until, start + datetime.timedelta(days=CHUNK_DAYS - 1))
        rows.extend(_fetch_range(account_id, start, end))
        start = end + datetime.timedelta(days=1)
    return rows


def _fetch_range(account_id: str, since: datetime.date, until: datetime.date) -> list:
    url = f"{GRAPH}/act_{account_id}/insights"
    params = {
        "level": "ad",
        "fields": INSIGHT_FIELDS,
        "time_increment": 1,
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        "limit": 200,
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
        # só conta como "já tem" se tiver as métricas novas (alcance etc.): senão busca os 90 dias de novo
        has_data = db.query(CampaignSpend.id).filter(
            CampaignSpend.tenant_id == tenant.id, CampaignSpend.source == "meta_api", CampaignSpend.reach > 0
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
            reach=int(r.get("reach") or 0),
            link_clicks=int(r.get("inline_link_clicks") or 0),
            # o Meta manda o mesmo número nos dois tipos: somar dobrava (taxa de carregamento de 178%)
            landing_page_views=_action(r, "landing_page_view") or _action(r, "omni_landing_page_view"),
            meta_results=_results(r),
        ))
    db.commit()
    try:
        sync_creatives(db, tenant)
    except (MetaError, requests.RequestException, ValueError) as exc:  # sem imagem não atrapalha o gasto
        db.rollback()
        logger.warning("meta_spend: criativos de %s falharam: %s", tenant.name, exc)
    return len(rows)


CREATIVE_DAYS = 90  # anúncios com gasto nesses dias aparecem na aba 🎨 Criativos
CREATIVE_REFRESH = datetime.timedelta(hours=20)  # o endereço da imagem do Meta expira: renova 1x por dia
CREATIVE_FIELDS = ("name,account_id,preview_shareable_link,"
                   "creative.thumbnail_width(600).thumbnail_height(600)"
                   "{object_type,image_url,thumbnail_url,video_id,instagram_permalink_url,body,object_story_spec}")


def _creative_image(creative: dict) -> str:
    """Melhor imagem do criativo: a da imagem; senão a do vídeo/carrossel; senão a miniatura."""
    if creative.get("image_url"):
        return creative["image_url"]
    story = creative.get("object_story_spec") or {}
    video = story.get("video_data") or {}
    if video.get("image_url"):
        return video["image_url"]
    for child in (story.get("link_data") or {}).get("child_attachments") or []:
        if child.get("picture"):
            return child["picture"]
    if (story.get("link_data") or {}).get("picture"):
        return story["link_data"]["picture"]
    return creative.get("thumbnail_url") or ""


def sync_creatives(db, tenant: Tenant, force: bool = False) -> int:
    """🎨 Imagem, texto e link de cada anúncio com gasto nos últimos CREATIVE_DAYS dias (só os que
    ainda não tem ou que buscou há mais de CREATIVE_REFRESH). Devolve quantos atualizou."""
    since = datetime.date.today() - datetime.timedelta(days=CREATIVE_DAYS)
    ad_ids = {a for (a,) in db.query(CampaignSpend.ad_id).filter(
        CampaignSpend.tenant_id == tenant.id, CampaignSpend.platform == "meta", CampaignSpend.ad_id != "",
        CampaignSpend.date >= since, CampaignSpend.spend > 0).distinct()}
    have = {c.ad_id: c for c in db.query(AdCreative).filter(AdCreative.tenant_id == tenant.id)}
    stale = datetime.datetime.utcnow() - CREATIVE_REFRESH
    todo = sorted(a for a in ad_ids if force or a not in have or have[a].fetched_at < stale)
    done = 0
    plain = CREATIVE_FIELDS.replace(".thumbnail_width(600).thumbnail_height(600)", "")
    fields = CREATIVE_FIELDS
    for ad_id in todo:
        # um anúncio por vez: o Meta não aceita mais "?ids=a,b,c" (Graph v26+)
        try:
            try:
                ad = _get(f"{GRAPH}/{ad_id}", {"fields": fields})
            except MetaError:
                if fields == plain:
                    raise
                fields = plain  # o Meta recusou o tamanho da miniatura: segue com a padrão (pros outros também)
                ad = _get(f"{GRAPH}/{ad_id}", {"fields": fields})
        except MetaError as exc:
            if "does not exist" in str(exc) or "Unsupported get request" in str(exc) or "permission" in str(exc).lower():
                continue  # anúncio apagado ou de uma conta a que o token não tem acesso
            raise
        if not ad:
            continue
        creative = ad.get("creative") or {}
        story = creative.get("object_story_spec") or {}
        kind = "video" if creative.get("video_id") or story.get("video_data") else (
            "carrossel" if (story.get("link_data") or {}).get("child_attachments") else "imagem")
        body = creative.get("body") or (story.get("link_data") or {}).get("message") or \
            (story.get("video_data") or {}).get("message") or ""
        row = have.get(ad_id) or AdCreative(tenant_id=tenant.id, ad_id=ad_id)
        row.account_id = str(ad.get("account_id") or "")
        row.name = (ad.get("name") or "")[:255]
        row.kind = kind
        row.image_url = _creative_image(creative)
        row.preview_url = ad.get("preview_shareable_link") or ""
        row.permalink = creative.get("instagram_permalink_url") or ""
        row.body = body[:2000]
        row.fetched_at = datetime.datetime.utcnow()
        db.add(row)
        have[ad_id] = row
        done += 1
        if done % 20 == 0:
            db.commit()  # vai aparecendo na tela enquanto busca o resto
    db.commit()
    return done


_creatives_running: set = set()


def refresh_creatives_now(tenant_id: str, force: bool = True) -> bool:
    """🔄 Busca as imagens e links dos anúncios do cliente agora, em segundo plano (botão da aba
    🎨 Criativos). False = já está buscando (ou sem token do Meta)."""
    if not META_ADS_ACCESS_TOKEN or tenant_id in _creatives_running:
        return False
    _creatives_running.add(tenant_id)

    def run():
        db = SessionLocal()
        try:
            tenant = db.get(Tenant, tenant_id)
            if tenant is not None:
                n = sync_creatives(db, tenant, force=force)
                logger.info("meta_spend: criativos de %s atualizados: %s", tenant.name, n)
        except Exception as exc:
            db.rollback()
            logger.warning("meta_spend: criativos (botão) falharam: %s", exc)
        finally:
            db.close()
            _creatives_running.discard(tenant_id)

    threading.Thread(target=run, daemon=True).start()
    return True


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
