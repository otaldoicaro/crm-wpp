"""Resolve a plataforma de origem de um lead (Google/Meta/Direto/etc) a partir
dos dados de atribuição, pra exibir um badge visual no pipeline/inbox/dashboard.
Mesma lógica usada em outros CRMs da agência (dashboard-brasil)."""

from __future__ import annotations

PLATFORM_LABEL = {
    "google": "Google",
    "meta": "Meta",
    "bing": "Bing",
    "tiktok": "TikTok",
    "organic": "Orgânico",
    "direct": "Direto",
}


def resolve_platform(attribution) -> str:
    if not attribution:
        return "direct"
    if attribution.ctwa_clid or attribution.gclid:
        return "google" if attribution.gclid else "meta"
    src = (attribution.utm_source or "").lower()
    medium = (attribution.utm_medium or "").lower()
    if "google" in src or "google" in medium:
        return "google"
    if any(k in src for k in ("facebook", "instagram", "meta", "fb", "ig")):
        return "meta"
    if "bing" in src:
        return "bing"
    if "tiktok" in src:
        return "tiktok"
    if src:
        return "organic"
    return "direct"


def platform_label(key: str) -> str:
    return PLATFORM_LABEL.get(key, "Direto")
