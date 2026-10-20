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
        "name": name, "platform": PLATFORM_LABEL.get(platform, platform), "leads": 0, "qualified": 0, "opportunities": 0,
        "won": 0, "revenue": 0.0, "spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "link_clicks": 0,
        "landing_page_views": 0, "meta_results": 0, "lp_link_clicks": 0, "page_leads": 0, "children": {},
    }


def _ratio(part, whole, scale: float = 1.0):
    return part / whole * scale if part is not None and whole else None


def _finish(node: dict) -> dict:
    kids = [_finish(child) for child in node["children"].values()]
    node["children"] = sorted(kids, key=lambda n: (n["spend"], n["leads"]), reverse=True)
    # leads de anúncio com página de destino (clique pro WhatsApp não tem página: fica de fora)
    if kids:
        node["page_leads"] = sum(k["page_leads"] for k in kids)
    elif node["key"] and node["landing_page_views"]:  # anúncio (o "Total" já vem somado)
        node["page_leads"] = node["leads"]
    spend, leads, won = node["spend"], node["leads"], node["won"]
    impressions, link_clicks, lpv = node["impressions"], node["link_clicks"], node["landing_page_views"]
    # mídia (Meta)
    node["cpm"] = _ratio(spend, impressions, 1000) if spend else None
    node["frequency"] = _ratio(impressions, node["reach"])  # média por dia (alcance somado dia a dia)
    node["ctr"] = _ratio(link_clicks, impressions, 100)  # CTR do link
    node["cpc"] = _ratio(spend, link_clicks) if spend else None  # CPC do link
    # página de destino
    node["load_rate"] = _ratio(lpv, node["lp_link_clicks"], 100) if lpv else None  # taxa de carregamento
    node["page_conv"] = _ratio(node["page_leads"], lpv, 100) if lpv else None  # leads no CRM ÷ visualizações
    node["cost_per_result"] = _ratio(spend, node["meta_results"]) if spend else None
    # comercial (CRM)
    node["cpl"] = spend / leads if spend and leads else None
    node["cost_per_qualified"] = spend / node["qualified"] if spend and node["qualified"] else None
    node["cpmql"] = node["cost_per_qualified"]
    node["cpo"] = spend / node["opportunities"] if spend and node["opportunities"] else None
    node["cac"] = spend / won if spend and won else None
    node["ticket"] = node["revenue"] / won if won else None
    node["roas"] = node["revenue"] / spend if spend else None
    node["conversion"] = 100 * won / leads if leads else None
    # taxas entre as etapas do funil
    node["mql_rate"] = _ratio(node["qualified"], leads, 100)
    node["opt_rate"] = _ratio(node["opportunities"], node["qualified"], 100)
    node["win_rate"] = _ratio(won, node["opportunities"], 100)
    return node


def build(db, tenant_id: str, leads: list, date_from, date_to, is_qualified, won_ids: set, is_opportunity=None) -> dict:
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
            if is_opportunity is not None and is_opportunity(lead):
                node["opportunities"] += 1
            if lead.stage_id in won_ids:
                node["won"] += 1
                node["revenue"] += lead.deal_value or 0

    spend_query = (
        db.query(
            CampaignSpend.platform, CampaignSpend.campaign_id, CampaignSpend.campaign, CampaignSpend.adset_id,
            CampaignSpend.adset, CampaignSpend.ad_id, CampaignSpend.ad,
            func.sum(CampaignSpend.spend), func.sum(CampaignSpend.impressions), func.sum(CampaignSpend.clicks),
            func.sum(CampaignSpend.reach), func.sum(CampaignSpend.link_clicks),
            func.sum(CampaignSpend.landing_page_views), func.sum(CampaignSpend.meta_results),
        )
        .filter(CampaignSpend.tenant_id == tenant_id)
    )
    if date_from:
        spend_query = spend_query.filter(CampaignSpend.date >= date_from)
    if date_to:
        spend_query = spend_query.filter(CampaignSpend.date <= date_to)
    has_spend = False
    for platform, cid, cname, sid, sname, aid, aname, spend, impressions, clicks, reach, link_clicks, lpv, results in spend_query.group_by(
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
            node["reach"] += reach or 0
            node["link_clicks"] += link_clicks or 0
            node["landing_page_views"] += lpv or 0
            if lpv:  # só anúncio com página de destino entra na taxa de carregamento
                node["lp_link_clicks"] += link_clicks or 0
            node["meta_results"] += results or 0

    total = _node("Total", "")
    for camp in campaigns.values():
        for key in ("leads", "qualified", "opportunities", "won", "revenue", "spend", "impressions", "clicks", "reach",
                    "link_clicks", "landing_page_views", "meta_results", "lp_link_clicks"):
            total[key] += camp[key]
    result = [_finish(c) for c in campaigns.values()]
    total["page_leads"] = sum(c["page_leads"] for c in result)
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


# ---------- métricas do Dashboard de tráfego (ordem = ordem das colunas) ----------
# (chave, nome, formato, melhor quando: "low" | "high" | None, grupo, explicação, como ajustar quando sai da meta)
METRICS = [
    # funil de vendas: investido -> leads -> MQL -> OPT -> venda (o que importa primeiro)
    ("spend", "Investido", "money0", None, "funil", "Quanto foi gasto no Meta no período.", ""),
    ("leads", "Leads", "int", None, "funil", "Leads de anúncio que chegaram no CRM.", ""),
    ("cpl", "CPL", "money", "low", "funil", "Investido ÷ leads.",
     "Lead caro: veja qual etapa piorou (CPM, CTR, carregamento ou conversão da página) e ajuste ela."),
    ("mql_rate", "Taxa MQL", "pct", "high", "funil", "Qualificados ÷ leads.",
     "Muito curioso e pouco cliente: deixe claro no anúncio o que vende (peça, carro, região) e revise o público."),
    ("qualified", "MQL", "int", None, "funil", "Leads que chegaram em Qualificado.", ""),
    ("cpmql", "CPMQL", "money", "low", "funil", "Investido ÷ qualificados.",
     "Muito lead desqualificado: ajuste o público e deixe claro no anúncio o que vende (peça, carro, região), pra filtrar curioso."),
    ("opt_rate", "Taxa OPT", "pct", "high", "funil", "Oportunidades ÷ qualificados (quantos MQL receberam preço).",
     "MQL que não recebe preço: veja o tempo de resposta e se o time está cotando rápido (estoque, foto da peça)."),
    ("opportunities", "OPT", "int", None, "funil", "Leads que receberam preço (chegaram em Negociando).", ""),
    ("cpo", "CPO", "money", "low", "funil", "Investido ÷ oportunidades.",
     "Poucos leads chegam a receber preço: veja o tempo de resposta e se o time está pedindo carro/peça logo no início."),
    ("win_rate", "Taxa de ganho", "pct", "high", "funil", "Vendas ÷ oportunidades (quantos que receberam preço compraram).",
     "Recebem preço e não compram: revise preço/condições, follow-up e os motivos de perda no Dashboard Comercial."),
    ("won", "Vendas", "int", None, "funil", "Leads que viraram venda.", ""),
    ("cac", "CAC", "money", "low", "funil", "Investido ÷ vendas.",
     "Venda cara: veja se o problema está no tráfego (CPL/CPMQL) ou no fechamento (Taxa OPT, Taxa de ganho)."),
    ("conversion", "Conversão em venda", "pct", "high", "funil", "Vendas ÷ leads.",
     "Poucos leads viram venda: revise atendimento, follow-up e motivos de perda no Dashboard Comercial."),
    ("revenue", "Faturamento", "money0", None, "funil", "Soma das vendas.", ""),
    ("ticket", "Ticket médio", "money", "high", "funil", "Faturamento ÷ vendas.",
     "Ticket baixo: ofereça itens complementares (kit, instalação) e destaque peças de maior valor nos anúncios."),
    ("roas", "ROAS", "x", "high", "funil", "Faturamento ÷ investido (2,0x = cada R$ 1 virou R$ 2).",
     "Retorno baixo: corte as campanhas/anúncios com pior CAC e coloque a verba nos que vendem."),
    # mídia (Meta)
    ("impressions", "Impressões", "int", None, "midia", "Quantas vezes os anúncios apareceram.", ""),
    ("cpm", "CPM", "money", "low", "midia", "Custo por mil impressões.",
     "Público pequeno ou disputado, ou anúncio com baixa relevância: amplie o público, teste posicionamentos automáticos e criativos novos."),
    ("frequency", "Frequência", "dec", "low", "midia", "Quantas vezes, em média, cada pessoa viu o anúncio por dia.",
     "As mesmas pessoas estão vendo demais: troque o criativo ou amplie o público."),
    ("link_clicks", "Cliques no link", "int", None, "midia", "Cliques que levaram pra página ou pro WhatsApp.", ""),
    ("ctr", "CTR (link)", "pct", "high", "midia", "Cliques no link ÷ impressões.",
     "O anúncio não chama atenção: teste outro gancho nos 3 primeiros segundos, outra imagem/vídeo e uma chamada mais clara."),
    ("cpc", "CPC (link)", "money", "low", "midia", "Investido ÷ cliques no link.",
     "Clique caro: geralmente CTR baixo ou CPM alto. Melhore o criativo ou o público."),
    # página de destino
    ("landing_page_views", "Visualizações da página", "int", None, "pagina", "Quantas pessoas carregaram a página de destino.", ""),
    ("load_rate", "Taxa de carregamento", "pct", "high", "pagina", "Visualizações da página ÷ cliques no link.",
     "Muita gente clica e desiste antes da página abrir: deixe a página mais leve e rápida no celular (imagens menores, menos scripts)."),
    ("page_conv", "Conversão da página", "pct", "high", "pagina", "Leads que chegaram no CRM ÷ visualizações da página.",
     "A página não convence: revise a oferta, o título, o formulário (menos campos) e o botão de WhatsApp."),
    ("meta_results", "Resultados (Meta)", "int", None, "pagina", "Cadastros + conversas iniciadas que o Meta contou.", ""),
]
METRIC = {m[0]: m for m in METRICS}
GOAL_METRICS = [m for m in METRICS if m[3]]  # as que têm "melhor quando" podem ter meta
METRIC_GROUPS = [("funil", "🤝 Funil de vendas"), ("midia", "📣 Mídia (Meta)"), ("pagina", "🌐 Página de destino")]


def goal_status(goals: dict, key: str, value) -> str:
    """"ok" | "bad" | "" (sem meta ou sem valor)."""
    goal = goals.get(key)
    meta = METRIC.get(key)
    if goal is None or value is None or not meta or not meta[3] or not goal.target:
        return ""
    good = value <= goal.target if meta[3] == "low" else value >= goal.target
    return "ok" if good else "bad"


def fmt(key: str, value) -> str:
    """Valor formatado do jeito brasileiro (R$ 1.234,56 · 2,4% · 1,8x)."""
    if value is None:
        return "—"
    kind = METRIC[key][2] if key in METRIC else "dec"

    def br(v, decimals):
        return f"{v:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    if kind == "money0":
        return f"R$ {br(value, 0)}" if value else "—"
    if kind == "money":
        return f"R$ {br(value, 2)}"
    if kind == "pct":
        return f"{br(value, 2 if value < 10 else 1)}%"
    if kind == "x":
        return f"{br(value, 1)}x"
    if kind == "dec":
        return br(value, 2)
    return br(value, 0)
