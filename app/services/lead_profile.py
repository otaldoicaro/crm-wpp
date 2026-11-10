"""📋 Ficha do lead por ramo (segments.py, "lead_profile"): os dados de qualificação (empresa, cargo,
faturamento, verba, nichos), as anotações do vendedor e os serviços vendidos. Fica em Lead.profile
(JSON); passa pro negócio novo quando o cliente volta (deals.open_new)."""

from __future__ import annotations

import json
import re

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
    for secao in cfg.get("secoes") or []:
        for campo in secao["campos"]:
            value = _field(form, campo)
            if value not in ("", None, False):
                data[campo[0]] = value
    return data


def _money(value: str):
    """"1.500,00", "1500", "R$ 1.500" → 1500.0 (vazio/inválido → None)."""
    text = re.sub(r"[^\d,.]", "", str(value or ""))
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or re.search(r"\.\d{3}$", text):
        text = text.replace(".", "")
    try:
        number = round(float(text), 2)
    except ValueError:
        return None
    return number if number >= 0 else None


def _field(form, campo):
    key, kind = campo[0], campo[2]
    raw = (form.get(key) or "").strip()
    if kind == "sim":
        return bool(raw)
    if kind == "dinheiro":
        return _money(raw)
    if kind == "inteiro":
        return _int(raw)
    if kind == "opcao":
        return raw if raw in campo[3] else ""
    if kind == "data":
        return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else ""
    if kind == "doc":
        return re.sub(r"[^\d./-]", "", raw)[:20]
    if kind == "longo":
        return raw[:1000]
    return raw[:300]


def company(lead) -> str:
    """Nome da empresa da ficha (vazio se não tiver ficha/empresa)."""
    if not lead.profile:
        return ""
    return str(load(lead).get("empresa") or "").strip()


def origin(lead, cfg) -> str:
    """Canal de origem: o que o vendedor marcou, ou o sugerido pelo jeito que o lead chegou."""
    profile = load(lead)
    return profile.get("origem") or (cfg or {}).get("origem_por_fonte", {}).get(lead.source or "", "")


def contract_total(profile: dict):
    """Setup + mensalidade × prazo (None se faltar a mensalidade ou o prazo)."""
    monthly, months = profile.get("mensalidade"), profile.get("prazo_meses")
    if not monthly or not months:
        return None
    return round((profile.get("setup") or 0) + monthly * months, 2)

