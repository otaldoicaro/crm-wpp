"""📋 Ficha do lead por ramo (segments.py, "lead_profile"): os dados de qualificação (empresa, cargo,
faturamento, verba, nichos), as anotações do vendedor e os serviços vendidos. Fica em Lead.profile
(JSON); passa pro negócio novo quando o cliente volta (deals.open_new)."""

from __future__ import annotations

import json

from app import segments


def config(tenant):
    """A ficha do ramo do cliente, ou None (ramo sem ficha)."""
    return segments.segment_for(tenant).get("lead_profile")


def load(lead) -> dict:
    try:
        data = json.loads(lead.profile or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _same(a: str, b: str) -> bool:
    """"R$50 mil a R$100 mil" (LP) = "R$ 50 mil a R$ 100 mil" (ficha): sem espaços nem maiúsculas."""
    return "".join(str(a).split()).lower() == "".join(str(b).split()).lower()


def fill_from_form(lead, tenant, data: dict) -> None:
    """Formulário da LP (empresa, site, cargo, faturamento, investimento) preenche a ficha. Não apaga o
    que o vendedor já escreveu: só completa o que está vazio."""
    cfg = config(tenant)
    if cfg is None:
        return
    profile = load(lead)
    picks = {"cargo": cfg["cargos"], "faturamento": cfg["faturamento"], "investimento": cfg["investimento"]}
    for key in ("empresa", "site", "cargo", "faturamento", "investimento"):
        value = str(data.get(key) or "").strip()[:300]
        if not value or profile.get(key):
            continue
        if key in picks:
            value = next((o for o in picks[key] if _same(o, value)), "")
        if value:
            profile[key] = value
    lead.profile = json.dumps(profile, ensure_ascii=False)


def _int(value: str):
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number > 0 else None


def from_form(form, cfg: dict) -> dict:
    """Lê o formulário da ficha. Só guarda o que está nas listas do ramo."""
    data = {key: (form.get(key) or "").strip()[:300] for key in ("empresa", "site", "cargo", "faturamento", "investimento")}
    for key, options in (("cargo", cfg["cargos"]), ("faturamento", cfg["faturamento"]), ("investimento", cfg["investimento"])):
        if data[key] not in options:
            data[key] = ""
    data["nichos"] = [n for n in cfg["nichos"] if n in form.getlist("nicho")]
    data["notas"] = (form.get("notas") or "").strip()[:5000]
    services = {}
    for key, label, kind in cfg["servicos"]:
        if not form.get("srv_" + key):
            continue
        if kind == "qtd":
            services[key] = _int(form.get("srv_" + key + "_qtd", "")) or 0
        elif kind == "freq":
            freq = form.get("srv_" + key + "_freq", "")
            services[key] = freq if freq in cfg["frequencias"] else ""
        else:
            services[key] = True
    data["servicos"] = services
    return data

