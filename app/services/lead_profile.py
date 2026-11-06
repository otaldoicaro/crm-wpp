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

