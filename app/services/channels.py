"""Canal de origem do lead, pros filtros do Dashboard: campanha de formulário (conv-form), campanha de
WhatsApp (conv-wpp), Meta, Google, sem anúncio. O tipo da campanha vem da etiqueta no NOME dela
("RN | peças-gerais | conv-form | ..."), que é o padrão das campanhas da agência.

Lead de clique pro WhatsApp só traz o id do anúncio: o nome da campanha vem do gasto já buscado no
Meta (campaign_spend). Sem nome nenhum, vale o jeito que o lead chegou: formulário = conv-form,
clique pro WhatsApp (ctwa) = conv-wpp."""

from __future__ import annotations

from sqlalchemy import func

from app.models import CampaignSpend
from app.services.platform import resolve_platform

CHANNELS = [
    ("", "Todos os canais"),
    ("form", "📝 Campanha de formulário (conv-form)"),
    ("wpp", "💬 Campanha de WhatsApp (conv-wpp)"),
    ("meta", "Só Meta (todas)"),
    ("google", "Só Google"),
    ("outros", "Sem anúncio (orgânico / direto)"),
]
CHANNEL_KEYS = {k for k, _ in CHANNELS}


def campaign_by_ad(db, tenant_id: str) -> dict:
    """{ad_id: nome da campanha} de todo o histórico de gasto do cliente."""
    rows = (db.query(CampaignSpend.ad_id, func.max(CampaignSpend.campaign))
            .filter(CampaignSpend.tenant_id == tenant_id, CampaignSpend.ad_id != "").group_by(CampaignSpend.ad_id).all())
    return {ad_id: name or "" for ad_id, name in rows}


def kind_of_campaign(name: str) -> str:
    """"form" | "wpp" | "" pela etiqueta no nome da campanha."""
    plain = (name or "").lower().replace("_", "-").replace(" ", "")
    if "conv-form" in plain or "convform" in plain:
        return "form"
    if "conv-wpp" in plain or "convwpp" in plain or "conv-whats" in plain:
        return "wpp"
    return ""


def lead_campaign(lead, by_ad: dict) -> str:
    att = lead.attribution
    if att is None:
        return ""
    return att.utm_campaign or by_ad.get(att.ad_id or "", "")


def lead_matches(lead, channel: str, by_ad: dict) -> bool:
    if not channel:
        return True
    platform = resolve_platform(lead.attribution)
    if channel == "meta":
        return platform == "meta"
    if channel == "google":
        return platform == "google"
    if channel == "outros":
        return platform in ("organic", "direct")
    kind = kind_of_campaign(lead_campaign(lead, by_ad))
    if not kind and platform == "meta":  # campanha sem a etiqueta no nome: pelo jeito que chegou
        att = lead.attribution
        kind = "wpp" if att is not None and att.ctwa_clid else ("form" if lead.source == "site_form" else "")
    return kind == channel


def spend_matches(campaign_name: str, platform: str, channel: str) -> bool:
    """Linha de gasto (campanha) entra no filtro? platform = "meta" | "google" | ..."""
    if not channel:
        return True
    if channel in ("meta", "google"):
        return platform == channel
    if channel == "outros":
        return False  # sem anúncio não tem gasto
    return kind_of_campaign(campaign_name) == channel
