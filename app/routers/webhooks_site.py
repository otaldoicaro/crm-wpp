"""Ingestão de leads vindos de formulário (site / landing page).

Dois formatos:
- POST /webhooks/site-form/{tenant_id}: formulário HTML comum (campos name, phone,
  email, utm_*, gclid, fbclid, fbc, fbp).
- POST /webhooks/lp/{tenant_id}: JSON (a LP da Nova Viseu manda com
  Content-Type text/plain + no-cors, pra não precisar de CORS). Campos da LP:
  nome, telefone/whatsapp, email, marca, modelo, ano, pecas, qtd_pecas_encontradas,
  veiculos, busca (LP de seminovos), empresa, site, cargo, faturamento, investimento (LP da
  agência), pagina, utm_*, origem {utm_*, gclid, fbclid}. Aceita também o mesmo conteúdo como
  formulário (application/x-www-form-urlencoded), que é como a LP da Junta manda.
  Com ?origem=leadster (chat do site), aceita qualquer formato: acha nome/telefone/e-mail pelo nome
  do campo em qualquer lugar do JSON (_from_chatbot).

Nos dois casos o lead é um por pessoa: se o telefone já existe (porque a pessoa
já chamou no WhatsApp, ou já preencheu antes), completa o mesmo lead em vez de
criar outro — ver app/services/lead_match.py.

A URL inclui o tenant_id como chave pública do formulário: não é secreta (fica
no código da página), o pior uso indevido seria criar leads falsos nesse cliente.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import Tenant
from app.services.lead_match import upsert_form_lead

router = APIRouter()
logger = logging.getLogger("webhooks_site")

CORS = {"Access-Control-Allow-Origin": "*"}


@router.post("/webhooks/site-form/{tenant_id}")
def receive_site_lead(
    tenant_id: str,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    utm_source: str = Form(""),
    utm_medium: str = Form(""),
    utm_campaign: str = Form(""),
    utm_content: str = Form(""),
    utm_term: str = Form(""),
    gclid: str = Form(""),
    fbclid: str = Form(""),
    fbc: str = Form(""),
    fbp: str = Form(""),
    db: Session = Depends(get_db),
):
    if not db.get(Tenant, tenant_id):
        return JSONResponse({"ok": False, "error": "tenant inválido"}, status_code=404)
    data = dict(
        name=name, phone=phone, email=email, utm_source=utm_source, utm_medium=utm_medium,
        utm_campaign=utm_campaign, utm_content=utm_content, utm_term=utm_term,
        gclid=gclid, fbclid=fbclid, fbc=fbc, fbp=fbp,
    )
    lead, created = upsert_form_lead(db, tenant_id, data, source="site_form")
    return {"ok": True, "lead_id": lead.id, "created": created}


@router.options("/webhooks/lp/{tenant_id}")
def lp_preflight(tenant_id: str):
    return JSONResponse({}, headers={**CORS, "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type"})


@router.post("/webhooks/lp/{tenant_id}")
async def receive_lp_lead(tenant_id: str, request: Request):
    try:
        raw = (await request.body()).decode("utf-8")
        if raw.lstrip()[:1] in ("{", ""):
            payload = json.loads(raw or "{}")
        else:  # formulário (nome=...&whatsapp=...)
            payload = {key: values[-1] for key, values in parse_qs(raw).items()}
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400, headers=CORS)

    if not isinstance(payload, dict):
        payload = {"respostas": payload}
    if request.query_params.get("origem", "").lower() == "leadster":
        payload = _from_chatbot(payload, "💬 Leadster (chat do site)")
    origem = payload.get("origem") if isinstance(payload.get("origem"), dict) else {}
    pick = lambda key: str(payload.get(key) or origem.get(key) or "")  # noqa: E731
    data = {
        "name": str(payload.get("nome") or payload.get("name") or ""),
        "phone": str(payload.get("whatsapp") or payload.get("telefone") or payload.get("phone") or ""),
        "email": str(payload.get("email") or ""),
        # ad_id: formulário nativo do Meta (via Make) — liga o lead ao gasto do anúncio
        **{k: pick(k) for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid", "fbc", "fbp", "ad_id")},
        **{k: payload.get(k) for k in ("marca", "modelo", "ano", "pecas", "qtd_pecas_encontradas", "veiculos", "busca", "mensagem", "pagina",
                                       "empresa", "site", "cargo", "faturamento", "investimento")},
        "respostas": payload.get("respostas"),  # perguntas do formulário nativo do Meta (lead_match.form_answers)
    }
    if not data["phone"] and not data["email"]:
        return JSONResponse({"ok": False, "error": "sem telefone nem e-mail"}, status_code=400, headers=CORS)
    result = await run_in_threadpool(_save_lp_lead, tenant_id, data)  # banco fora do laço principal
    return JSONResponse(result, status_code=200 if result["ok"] else 404, headers=CORS)


NAME_KEYS = ("nome", "name", "nome_completo", "full_name", "fullname", "seu_nome", "lead_name", "nome_do_lead")
PHONE_KEYS = ("whatsapp", "telefone", "phone", "celular", "phone_number", "telephone", "mobile", "fone", "tel", "telefone_celular",
              "numero_whatsapp", "whats", "lead_phone")
EMAIL_KEYS = ("email", "e-mail", "e_mail", "mail", "lead_email", "seu_email")
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid", "fbc", "fbp")


def _flat(value, prefix: str = "", out: dict | None = None) -> dict:
    """{"lead": {"Nome": "Ana"}} -> {"nome": "Ana"} (chave em minúsculas, sem o caminho)."""
    out = {} if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            _flat(item, str(key), out)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and any(k in item for k in ("question", "label", "name", "pergunta", "campo", "field")) \
                    and any(k in item for k in ("answer", "value", "values", "resposta", "valor")):
                question = item.get("question") or item.get("label") or item.get("pergunta") or item.get("campo") or item.get("field") or item.get("name")
                answer = item.get("answer") or item.get("resposta") or item.get("valor") or item.get("value") or item.get("values")
                _flat(answer if not isinstance(answer, list) else ", ".join(map(str, answer)), str(question), out)
            else:
                _flat(item, prefix, out)
    elif value not in (None, "") and prefix:
        key = " ".join(prefix.strip().lower().replace("-", "_").split()).replace(" ", "_")
        if key == "e_mail":
            key = "email"
        out.setdefault(key, str(value).strip())
    return out


def _from_chatbot(payload: dict, page: str) -> dict:
    """Chat do site (Leadster): o formato muda conforme a conta, então acha nome/telefone/e-mail pelo
    nome do campo em qualquer lugar do JSON; o resto vira as respostas na ficha do lead."""
    flat = _flat(payload)
    logger.info("lead do chat do site (%s): campos %s", page, sorted(flat)[:40])
    pick = lambda keys: next((flat[k] for k in keys if flat.get(k)), "")  # noqa: E731
    used = set(NAME_KEYS) | set(PHONE_KEYS) | set(EMAIL_KEYS) | set(UTM_KEYS)
    phone = pick(PHONE_KEYS)
    if not phone:  # campo com outro nome, mas com cara de telefone
        phone = next((v for v in flat.values() if 10 <= len(re.sub(r"\D", "", v)) <= 13 and re.fullmatch(r"[\d\s()+\-.]+", v)), "")
    email = pick(EMAIL_KEYS) or next((v for v in flat.values() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", v, re.I)), "")
    profile = {field: pick(keys) for field, keys in PROFILE_KEYS.items()}  # 📋 ficha (ramo agência)
    used |= {k for keys in PROFILE_KEYS.values() for k in keys} | {"url", "page_url", "pagina"}
    answers = {k: v for k, v in flat.items() if k not in used and not _technical(k, v) and len(v) <= 500 and v not in (phone, email)}
    return {"nome": pick(NAME_KEYS), "whatsapp": phone, "email": email,
            "pagina": page + (f" · {flat.get('url') or flat.get('page_url') or flat.get('pagina')}" if (flat.get("url") or flat.get("page_url") or flat.get("pagina")) else ""),
            "respostas": dict(list(answers.items())[:25]), **{k: v for k, v in profile.items() if v},
            **{k: flat[k] for k in UTM_KEYS if flat.get(k)}}


# campos do chat que viram a 📋 ficha do lead (mesmos nomes do formulário da LP)
PROFILE_KEYS = {
    "empresa": ("empresa", "nome_empresa", "nome_da_empresa", "company", "company_name"),
    "site": ("site", "siteinstagram", "site_instagram", "instagram", "site_ou_instagram", "website"),
    "cargo": ("cargo", "job_title", "funcao"),
    "faturamento": ("faturamento", "faturamentoempresa", "faturamento_empresa", "faturamento_mensal"),
    "investimento": ("investimento", "investimentotrafego", "investimento_trafego", "investimento_em_marketing"),
}
# dados internos do chat (ids, datas, IP, regras do robô...): não vão pra ficha do lead
TECHNICAL = {"id", "lead_id", "conversation_id", "created_at", "updated_at", "token", "type", "event", "status", "conversion",
             "score", "ip", "ip_address", "uid", "channel", "default", "enabled", "migrated", "lead_source", "script_identifier",
             "lead_created_at", "lead_created_date", "user_agent", "referrer", "timezone"}


def _technical(key: str, value: str) -> bool:
    return (key in TECHNICAL or key.endswith(("_id", "_at", "_date", "sended?", "sended", "_sent")) or key.startswith(("openmo", "source_script"))
            or value in ("True", "False", "true", "false"))


def _save_lp_lead(tenant_id: str, data: dict) -> dict:
    db = SessionLocal()
    try:
        if not db.get(Tenant, tenant_id):
            return {"ok": False, "error": "tenant inválido"}
        lead, created = upsert_form_lead(db, tenant_id, data, source="site_form")
        return {"ok": True, "lead_id": lead.id, "created": created}
    finally:
        db.close()
