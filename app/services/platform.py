"""Resolve a plataforma de origem de um lead (Google/Meta/Direto/etc) a partir
dos dados de atribuição, pra exibir um badge visual no pipeline/inbox/dashboard.
Mesma lógica usada em outros CRMs da agência (dashboard-brasil)."""

from __future__ import annotations

import re

PLATFORM_LABEL = {
    "google": "Google",
    "meta": "Meta",
    "bing": "Bing",
    "tiktok": "TikTok",
    "organic": "Orgânico",
    "direct": "Direto",
}


META_WORDS = ("facebook", "instagram", "meta", "messenger", "audience_network", "threads")
META_TOKENS = {"fb", "ig", "an", "msg", "th"}  # {{site_source_name}} do Meta: fb-..., ig-..., an-..., msg-...
PAID_MEDIUMS = {"cpc", "ppc", "paid", "pago", "ads", "paid_social", "paidsocial", "cpm", "display"}


def resolve_platform(attribution) -> str:
    if not attribution:
        return "direct"
    if attribution.gclid:
        return "google"
    # sinais do Meta mesmo sem UTM: clique no anúncio de WhatsApp, fbclid/cookie _fbc do site,
    # id do anúncio (formulário nativo via Make)
    if attribution.ctwa_clid or attribution.fbclid or attribution.fbc or attribution.ad_id:
        return "meta"
    src = (attribution.utm_source or "").lower()
    medium = (attribution.utm_medium or "").lower()
    tokens = set(re.split(r"[^a-z0-9]+", src))
    if "google" in src or "google" in medium or "gads" in tokens or "adwords" in src:
        return "google"
    if any(w in src for w in META_WORDS) or tokens & META_TOKENS:
        return "meta"
    if "bing" in src or "microsoft" in src:
        return "bing"
    if "tiktok" in src:
        return "tiktok"
    if src or medium:
        return "organic"
    return "direct"


def platform_label(key: str) -> str:
    return PLATFORM_LABEL.get(key, "Direto")
