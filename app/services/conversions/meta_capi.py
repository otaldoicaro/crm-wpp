"""📤 Eventos do CRM pro Meta (API de Conversões), um cliente nunca misturado com outro.

Por que via pixel/conjunto de dados (e não "business_messaging"): o envio oficial pra anúncio de
clique pro WhatsApp exige a API oficial do WhatsApp (Cloud API). Com o WhatsApp do celular
(Evolution), o caminho é o conjunto de dados do cliente, com telefone/e-mail criptografados
(SHA-256): o Meta cruza com quem viu/clicou no anúncio.

Travas (o algoritmo de um cliente nunca aprende com dado de outro):
- pixel e token são do CLIENTE (Tenant), nunca uma configuração geral do servidor;
- ao salvar, o pixel é conferido no Meta: tem que estar ligado a uma das contas de anúncio
  DESTE cliente (Tenant.meta_ad_accounts). Sem conferência, nada sai;
- cada evento leva o pixel de destino gravado (ConversionEvent.destination) pra auditoria, e na
  hora de enviar confere de novo que o lead é do mesmo cliente do pixel.
"""

from __future__ import annotations

import hashlib
import re
import time

import requests

from app.config import META_ADS_ACCESS_TOKEN, META_GRAPH_VERSION

GRAPH = f"https://graph.facebook.com/{META_GRAPH_VERSION}"
# etapa do CRM -> evento padrão do Meta (padrão = dá pra otimizar campanha e ver no Gerenciador)
EVENT_FOR_KIND = {"opportunity": "InitiateCheckout", "won": "Purchase"}
KIND_LABEL = {"opportunity": "Oportunidade (Negociando)", "won": "Venda (Ganho)"}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _phone_e164(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) in (10, 11):  # sem o 55
        digits = "55" + digits
    return digits


def _clean_ids(text: str) -> list:
    return [p.strip().replace("act_", "") for p in re.split(r"[\s,;]+", text or "") if p.strip()]


def verify_pixel(tenant) -> tuple:
    """(ok, mensagem). O pixel tem que aparecer nas contas de anúncio DESTE cliente, e o token
    do cliente tem que conseguir ler esse pixel."""
    pixel = (tenant.meta_pixel_id or "").strip()
    if not pixel.isdigit():
        return False, "Coloque o ID do conjunto de dados/pixel (só números)."
    if not tenant.meta_capi_token:
        return False, "Coloque o token de acesso gerado nesse pixel."
    accounts = _clean_ids(tenant.meta_ad_accounts)
    if not accounts:
        return False, "Este cliente ainda não tem contas de anúncio cadastradas: sem elas não dá pra conferir de quem é o pixel."
    if not META_ADS_ACCESS_TOKEN:
        return False, "Falta o token do Meta Ads no servidor pra conferir o pixel."
    found_in = []
    for account in accounts:
        try:
            resp = requests.get(f"{GRAPH}/act_{account}/adspixels", params={
                "fields": "id,name", "limit": 200, "access_token": META_ADS_ACCESS_TOKEN}, timeout=20)
            data = resp.json()
        except (requests.RequestException, ValueError):
            continue
        if any(str(p.get("id")) == pixel for p in data.get("data", [])):
            found_in.append(account)
    if not found_in:
        return False, ("Esse pixel NÃO está ligado a nenhuma conta de anúncio deste cliente "
                       f"({', '.join(accounts)}). Confira se não é o pixel de outro cliente.")
    try:
        resp = requests.get(f"{GRAPH}/{pixel}", params={"fields": "id,name", "access_token": tenant.meta_capi_token}, timeout=20)
        body = resp.json()
    except (requests.RequestException, ValueError):
        return False, "Não consegui falar com o Meta pra testar o token. Tente de novo."
    if not resp.ok:
        return False, "O token não tem acesso a esse pixel: gere o token dentro desse conjunto de dados no Gerenciador de Eventos."
    return True, f"Pixel “{body.get('name', pixel)}” conferido: é da conta de anúncio {', '.join(found_in)} deste cliente."


def build_event(lead, kind: str, event_id: str, event_time: int, waba_id: str = "") -> dict:
    """Evento com os dados do lead (telefone/e-mail/nome criptografados) e, se veio pelo site,
    os identificadores do clique (fbc/fbp). Lead de clique pro WhatsApp + conta do WhatsApp (WABA)
    configurada: vai como "business_messaging" com o ctwa_clid (o formato de otimização do Meta)."""
    att = lead.attribution
    if waba_id and att and att.ctwa_clid:
        custom = {"currency": "BRL"}
        value = lead.deal_value if kind == "won" else lead.quoted_value
        if value:
            custom["value"] = round(float(value), 2)
        return {
            "event_name": EVENT_FOR_KIND[kind],
            "event_time": event_time,
            "event_id": event_id,
            "action_source": "business_messaging",
            "messaging_channel": "whatsapp",
            "user_data": {"whatsapp_business_account_id": waba_id, "ctwa_clid": att.ctwa_clid},
            "custom_data": custom,
        }
    user = {"country": [_sha256("br")], "external_id": [_sha256(lead.contact_id or lead.id)]}
    phone = _phone_e164(lead.phone)
    if phone:
        user["ph"] = [_sha256(phone)]
    if lead.email:
        user["em"] = [_sha256(lead.email)]
    first = (lead.name or "").strip().split(" ")[0]
    if first and first.isalpha():
        user["fn"] = [_sha256(first)]
    fbc = (att.fbc if att else "") or (f"fb.1.{int(lead.created_at.timestamp() * 1000)}.{att.fbclid}" if att and att.fbclid else "")
    if fbc:
        user["fbc"] = fbc
    if att and att.fbp:
        user["fbp"] = att.fbp
    custom = {"currency": "BRL"}
    if kind == "won":
        custom["value"] = round(float(lead.deal_value or 0), 2)
    elif lead.quoted_value:
        custom["value"] = round(float(lead.quoted_value), 2)
    return {
        "event_name": EVENT_FOR_KIND[kind],
        "event_time": event_time,
        "event_id": event_id,
        # veio pelo site (tem clique do Meta): "website"; conversa de WhatsApp: "chat"
        "action_source": "website" if fbc or (att and att.fbp) else "chat",
        "user_data": user,
        "custom_data": custom,
    }


def send(tenant, events: list) -> dict:
    """Manda pro pixel DO CLIENTE. {"ok", "status_code", "body"}; nunca levanta exceção."""
    if not (tenant.meta_capi_enabled and tenant.meta_pixel_id and tenant.meta_capi_token and tenant.meta_capi_checked_at):
        return {"ok": False, "status_code": 0, "body": {"error": "envio desligado ou pixel não conferido pra este cliente"}}
    payload = {"data": events}
    if tenant.meta_capi_test_code:
        payload["test_event_code"] = tenant.meta_capi_test_code  # aparece só em "Eventos de teste"
    try:
        resp = requests.post(f"{GRAPH}/{tenant.meta_pixel_id}/events", params={"access_token": tenant.meta_capi_token},
                             json=payload, timeout=20)
        body = resp.json() if resp.content else {}
        return {"ok": resp.ok, "status_code": resp.status_code, "body": body}
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "status_code": 0, "body": {"error": str(exc)}}


def send_test(tenant) -> dict:
    """Evento de teste (precisa do código de teste): confere o caminho todo sem sujar os dados."""
    if not tenant.meta_capi_test_code:
        return {"ok": False, "status_code": 0, "body": {"error": "coloque o código de teste do Gerenciador de Eventos"}}
    event = {
        "event_name": "InitiateCheckout", "event_time": int(time.time()), "event_id": f"teste-{int(time.time())}",
        "action_source": "chat", "user_data": {"ph": [_sha256("5511999999999")], "country": [_sha256("br")]},
        "custom_data": {"currency": "BRL", "value": 1.0},
    }
    return send(tenant, [event])


def verify_waba(tenant, number) -> tuple:
    """(ok, mensagem, dataset_id). O WABA tem que ter ESTE telefone (o do número no CRM) e um
    conjunto de dados; o token do cliente precisa de acesso (whatsapp_business_manage_events)."""
    waba = (number.meta_waba_id or "").strip()
    if not waba.isdigit():
        return False, "Coloque o ID da conta do WhatsApp (só números).", ""
    if not tenant.meta_capi_token:
        return False, "Salve primeiro o token de acesso (acima).", ""
    try:
        resp = requests.get(f"{GRAPH}/{waba}/phone_numbers", params={
            "fields": "display_phone_number", "access_token": tenant.meta_capi_token}, timeout=20)
        phones = resp.json()
    except (requests.RequestException, ValueError):
        return False, "Não consegui falar com o Meta agora. Tente de novo.", ""
    if not resp.ok:
        return False, ("O token não tem acesso a essa conta do WhatsApp: dê acesso ao usuário do sistema "
                       "(permissão de eventos do WhatsApp) e gere o token de novo."), ""
    mine = re.sub(r"\D", "", number.phone_number or "")
    listed = [re.sub(r"\D", "", p.get("display_phone_number", "")) for p in phones.get("data", [])]
    if not any(p and (p == mine or p[-8:] == mine[-8:]) for p in listed):
        return False, f"Essa conta do WhatsApp não tem o número {number.phone_number} (tem: {', '.join(listed) or 'nenhum'}).", ""
    try:
        resp = requests.get(f"{GRAPH}/{waba}/dataset", params={"access_token": tenant.meta_capi_token}, timeout=20)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return False, "Não consegui ler o conjunto de dados dessa conta do WhatsApp.", ""
    ids = [str(d.get("id")) for d in (data.get("data") or []) if d.get("id")] or ([str(data["id"])] if data.get("id") else [])
    if not resp.ok or not ids:
        return False, "Essa conta do WhatsApp ainda não tem conjunto de dados (crie no Gerenciador de Eventos ligando a conta do WhatsApp).", ""
    return True, f"Conta do WhatsApp {waba} conferida (número {number.phone_number}, conjunto de dados {ids[0]}).", ids[0]


def send_to(tenant, destination: str, events: list) -> dict:
    """Manda pra um destino do cliente (pixel ou conjunto de dados de um WhatsApp dele)."""
    if not (tenant.meta_capi_enabled and tenant.meta_capi_token):
        return {"ok": False, "status_code": 0, "body": {"error": "envio desligado pra este cliente"}}
    payload = {"data": events}
    if tenant.meta_capi_test_code:
        payload["test_event_code"] = tenant.meta_capi_test_code
    try:
        resp = requests.post(f"{GRAPH}/{destination}/events", params={"access_token": tenant.meta_capi_token},
                             json=payload, timeout=20)
        body = resp.json() if resp.content else {}
        return {"ok": resp.ok, "status_code": resp.status_code, "body": body}
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "status_code": 0, "body": {"error": str(exc)}}
