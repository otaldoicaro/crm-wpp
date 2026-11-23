"""🤝 Selo de campanha no lead: campanha com uma sigla no nome (utm_campaign) ganha um selo no topo da
conversa, na ficha e no card do Pipeline. Ex: campanhas de parceiros (oficinas, mecânicas) com "PARC"
no nome -> selo "🤝 PARCEIROS".

Cada cliente escreve as regras em ⚙ Configurações → Prazos → 🤝 Selos de campanha (Tenant.campaign_tags),
uma por linha: SIGLA = Selo. A sigla é procurada como palavra inteira no nome da campanha, sem ligar pra
maiúscula/acento: "PARC" pega "[PARC] Oficinas RJ" e "parc_mecanicas", mas não "Parcelado em 10x"."""

from __future__ import annotations

import re
import unicodedata

_cache: dict = {}


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower()) if unicodedata.category(c) != "Mn")


def rules(raw: str) -> list:
    """[(regex, selo)] das linhas "SIGLA = Selo" (linha sem "=": o selo é a própria sigla)."""
    if raw not in _cache:
        out = []
        for line in (raw or "").splitlines():
            key, _, label = line.partition("=")
            key, label = _plain(key).strip(), (label.strip() or key.strip())
            if key and label:
                out.append((re.compile(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"), label[:40]))
        _cache[raw] = out
    return _cache[raw]


def tags_for(tenant, lead) -> list:
    """Selos do lead (em ordem, sem repetir)."""
    raw = getattr(tenant, "campaign_tags", "") or ""
    att = getattr(lead, "attribution", None) if raw.strip() else None
    campaign = _plain(getattr(att, "utm_campaign", "") or "") if att is not None else ""
    if not campaign:
        return []
    found = []
    for pattern, label in rules(raw):
        if pattern.search(campaign) and label not in found:
            found.append(label)
    return found
