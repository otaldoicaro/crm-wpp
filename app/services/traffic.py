"""Tráfego pago x comercial: junta o gasto dos anúncios (campaign_spend) com os leads e
vendas de cada campanha > conjunto > anúncio.

O lead de "clique pro WhatsApp" traz o id do anúncio: casa com o gasto pelo id (mesmo se a
campanha for renomeada). Lead de site/LP só tem os nomes (UTMs): casa pelo nome."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func

from app.models import CampaignSpend
from app.services.platform import PLATFORM_LABEL, resolve_platform


def _key(parts: tuple) -> str:
    """Chave da campanha/conjunto/anúncio pra usar no link "ver leads"."""
    return ":".join(str(p) for p in parts)


def _node(name: str, platform: str, key: str = "") -> dict:
    return {
        "key": key, "lead_ids": [],
        "name": name, "platform": PLATFORM_LABEL.get(platform, platform), "leads": 0, "qualified": 0,
        "won": 0, "revenue": 0.0, "spend": 0.0, "impressions": 0, "clicks": 0, "children": {},
    }


def _finish(node: dict) -> dict:
    spend, leads, won = node["spend"], node["leads"], node["won"]
    node["cpl"] = spend / leads if spend and leads else None
    node["cost_per_qualified"] = spend / node["qualified"] if spend and node["qualified"] else None
    node["cac"] = spend / won if spend and won else None
    node["ticket"] = node["revenue"] / won if won else None
    node["roas"] = node["revenue"] / spend if spend else None
    node["conversion"] = 100 * won / leads if leads else None
    kids = [_finish(child) for child in node["children"].values()]
    node["children"] = sorted(kids, key=lambda n: (n["spend"], n["leads"]), reverse=True)
    return node


def build(db, tenant_id: str, leads: list, date_from, date_to, is_qualified, won_ids: set) -> dict:
    """{"campaigns": [...], "total": {...}, "has_spend": bool}. Cada campanha tem
    "children" (conjuntos) e cada conjunto tem "children" (anúncios)."""
    # nomes e ids conhecidos (todo o histórico, pra casar leads de qualquer data)
    known = (
        db.query(
            CampaignSpend.ad_id, func.max(CampaignSpend.campaign_id), func.max(CampaignSpend.adset_id),
            func.max(CampaignSpend.campaign), func.max(CampaignSpend.adset), func.max(CampaignSpend.ad),
        )
        .filter(CampaignSpend.tenant_id == tenant_id, CampaignSpend.ad_id != "")
        .group_by(CampaignSpend.ad_id)
        .all()
    )
    by_ad = {row[0]: row[1:] for row in known}
    campaign_by_name = {names[2]: names[0] for names in by_ad.values()}
    adset_by_name = {(names[0], names[3]): names[1] for names in by_ad.values()}
    ad_by_name = {(names[1], names[4]): ad_id for ad_id, names in by_ad.items()}

    campaigns: dict = {}

    def path(platform, cid, cname, sid, sname, aid, aname):
        ckey = ("id", cid) if cid else ("n", platform, cname)
        camp = campaigns.setdefault(ckey, _node(cname or "(sem nome)", platform, _key(ckey)))
        skey = ("id", sid) if sid else ("n", sname)
        adset = camp["children"].setdefault(skey, _node(sname or "(sem conjunto)", platform, _key(skey)))
        akey = ("id", aid) if aid else ("n", aname)
        ad = adset["children"].setdefault(akey, _node(aname or "(sem anúncio)", platform, _key(akey)))
        return camp, adset, ad

    for lead in leads:
        att = lead.attribution
        if att is None:
            continue
        if att.ad_id and att.ad_id in by_ad:
            cid, sid, cname, sname, aname = by_ad[att.ad_id]
            nodes = path("meta", cid, cname, sid, sname, att.ad_id, aname)
        elif att.utm_campaign:
            platform = "meta" if att.ad_id else resolve_platform(att)
            cname, sname, aname = att.utm_campaign, att.utm_term, att.utm_content
            cid = campaign_by_name.get(cname, "")
            sid = adset_by_name.get((cid, sname), "") if cid else ""
            aid = ad_by_name.get((sid, aname), "") if sid else ""
            nodes = path(platform, cid, cname, sid, sname, aid, aname)
        else:
            continue
        for node in nodes:
            node["lead_ids"].append(lead.id)
            node["leads"] += 1
            if is_qualified(lead):
                node["qualified"] += 1
            if lead.stage_id in won_ids:
                node["won"] += 1
                node["revenue"] += lead.deal_value or 0

    spend_query = (
        db.query(
            CampaignSpend.platform, CampaignSpend.campaign_id, CampaignSpend.campaign, CampaignSpend.adset_id,
            CampaignSpend.adset, CampaignSpend.ad_id, CampaignSpend.ad,
            func.sum(CampaignSpend.spend), func.sum(CampaignSpend.impressions), func.sum(CampaignSpend.clicks),
        )
        .filter(CampaignSpend.tenant_id == tenant_id)
    )
    if date_from:
        spend_query = spend_query.filter(CampaignSpend.date >= date_from)
    if date_to:
        spend_query = spend_query.filter(CampaignSpend.date <= date_to)
    has_spend = False
    for platform, cid, cname, sid, sname, aid, aname, spend, impressions, clicks in spend_query.group_by(
        CampaignSpend.platform, CampaignSpend.campaign_id, CampaignSpend.campaign, CampaignSpend.adset_id,
        CampaignSpend.adset, CampaignSpend.ad_id, CampaignSpend.ad,
    ):
        if aid and aid in by_ad:  # nome mais recente (a campanha pode ter sido renomeada)
            cid, sid, cname, sname, aname = by_ad[aid]
        has_spend = has_spend or bool(spend)
        for node in path(platform, cid, cname, sid, sname, aid, aname):
            node["spend"] += spend or 0
            node["impressions"] += impressions or 0
            node["clicks"] += clicks or 0

    total = _node("Total", "")
    for camp in campaigns.values():
        for key in ("leads", "qualified", "won", "revenue", "spend", "impressions", "clicks"):
            total[key] += camp[key]
    result = [_finish(c) for c in campaigns.values()]
    result.sort(key=lambda n: (n["spend"], n["leads"]), reverse=True)
    return {"campaigns": result, "total": _finish(total), "has_spend": has_spend}


def find(tree: dict, c: str, s: str = "", a: str = "") -> Optional[list]:
    """[campanha, conjunto?, anúncio?] pelo caminho de chaves do link "ver leads"."""
    camp = next((n for n in tree["campaigns"] if n["key"] == c), None)
    if camp is None:
        return None
    trail = [camp]
    if s:
        adset = next((n for n in camp["children"] if n["key"] == s), None)
        if adset is None:
            return None
        trail.append(adset)
        if a:
            ad = next((n for n in adset["children"] if n["key"] == a), None)
            if ad is None:
                return None
            trail.append(ad)
    return trail
