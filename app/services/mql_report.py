"""🎯 Faixas de MQL no Dashboard (recurso "dash_mql", ramo com ficha de agência).

Quantos leads do período responderam cada faixa de faturamento, quais campanhas trouxeram cada faixa,
e a lista dos leads (faturamento, investimento, plataforma, campanha, etapa) pra filtrar na tela.

A resposta vem da ficha ou do formulário/Leadster (lead_profile._answers) e é encaixada nas faixas da
ficha do ramo pelo começo da faixa: "R$100 mil a R$200 mil" (formulário) cai em "R$ 100 mil a R$ 250 mil"."""

from __future__ import annotations

from app.services import lead_profile
from app.services.platform import PLATFORM_LABEL, resolve_platform

NO_ANSWER = "Não respondeu"


def _bucket(text: str, options: list) -> str:
    """Encaixa a resposta numa faixa da ficha (a mesma escrita, ou pelo começo da faixa)."""
    if not text:
        return NO_ANSWER
    for option in options:
        if lead_profile._same(option, text):
            return option
    value = lead_profile.floor_value(text)
    if value is None:
        return NO_ANSWER
    best = ""
    for option in options:
        floor = lead_profile.floor_value(option)
        if floor is not None and floor <= value:
            best = option  # as faixas da ficha estão em ordem crescente
    return best or options[0]


def campaign_by_lead(traffic_data: dict) -> dict:
    """{lead_id: nome da campanha} a partir do 📣 Tráfego (mesmo cruzamento: gclid, anúncio, UTM)."""
    out = {}
    for camp in (traffic_data or {}).get("campaigns", []):
        for lead_id in camp["lead_ids"]:
            out.setdefault(lead_id, camp["name"])
    return out


def build(leads: list, cfg: dict, campaigns: dict, is_qualified) -> dict:
    revenue_opts, invest_opts = cfg["faturamento"], cfg["investimento"]
    rows = []
    for lead in leads:
        answers = lead_profile._answers(lead)
        platform = resolve_platform(lead.attribution)
        rows.append({
            "lead": lead,
            "faturamento": _bucket(answers.get("faturamento", ""), revenue_opts),
            "investimento": _bucket(answers.get("investimento", ""), invest_opts),
            "platform": platform,
            "platform_label": PLATFORM_LABEL.get(platform, platform),
            "campaign": campaigns.get(lead.id, ""),
            "mql": bool(is_qualified(lead)),
        })

    # resumo: uma linha por faixa de faturamento (maior primeiro), com plataformas e campanhas
    order = list(reversed(revenue_opts)) + [NO_ANSWER]
    summary = []
    for faixa in order:
        group = [r for r in rows if r["faturamento"] == faixa]
        if not group and faixa == NO_ANSWER:
            continue
        by_platform: dict = {}
        by_campaign: dict = {}
        for r in group:
            by_platform[r["platform_label"]] = by_platform.get(r["platform_label"], 0) + 1
            name = r["campaign"] or "(sem campanha)"
            by_campaign[name] = by_campaign.get(name, 0) + 1
        summary.append({
            "faixa": faixa,
            "leads": len(group),
            "mql": sum(1 for r in group if r["mql"]),
            "share": round(100 * len(group) / len(rows), 1) if rows else 0,
            "platforms": sorted(by_platform.items(), key=lambda kv: kv[1], reverse=True),
            "campaigns": sorted(by_campaign.items(), key=lambda kv: kv[1], reverse=True),
        })

    # lista: quem respondeu faixas maiores primeiro, depois os mais novos
    rank = {faixa: i for i, faixa in enumerate(order)}
    rows.sort(key=lambda r: (rank.get(r["faturamento"], len(order)), -(r["lead"].created_at.timestamp())))
    return {
        "summary": summary,
        "rows": rows,
        "total": len(rows),
        "answered": sum(1 for r in rows if r["faturamento"] != NO_ANSWER),
        "mql_total": sum(1 for r in rows if r["mql"]),
        "faixas": order,
        "investimentos": list(reversed(invest_opts)) + [NO_ANSWER],
        "platforms": sorted({r["platform_label"] for r in rows}),
        "campaigns": sorted({r["campaign"] for r in rows if r["campaign"]}),
    }
