"""Captura de origem dos leads.

Três caminhos diferentes convergem para o mesmo registro (UtmAttribution):

1. Formulário do site: UTMs + fbclid/gclid vêm direto no POST do formulário
   (o site precisa repassar os parâmetros da URL, ver `parse_site_form_utms`).

2. Anúncio "Clique para WhatsApp" da Meta: o próprio webhook da Cloud API
   entrega um objeto `referral` na primeira mensagem com `ctwa_clid`, id do
   anúncio, etc. Não precisa de nenhuma ponte — é nativo.

3. Google Ads -> WhatsApp: não existe equivalente nativo, então usamos uma
   landing page (bridge) que guarda o gclid/UTMs num `ClickBridge` com um
   código curto, embute esse código na mensagem pré-preenchida do wa.me, e
   aqui casamos o código quando a mensagem chega no webhook.
"""

from __future__ import annotations

import random
import re
import string

from sqlalchemy.orm import Session

from app.models import ClickBridge, Lead, UtmAttribution
from app.services.meta_ads_lookup import fetch_ad_names

TRACKING_TAG_RE = re.compile(r"#([A-Z0-9]{6,10})\b")


def generate_tracking_code(db: Session, length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=length))
        if not db.query(ClickBridge).filter(ClickBridge.tracking_code == code).first():
            return code


def build_whatsapp_bridge_link(phone_e164: str, tracking_code: str, prefilled_text: str) -> str:
    """Monta o link wa.me com o texto pré-preenchido + código de rastreio embutido.

    O código vai visível no fim da mensagem (ex: "Quero saber mais! #A1B2C3D4").
    Isso é intencional: precisa sobreviver ao envio real da mensagem para o
    número aparecer no corpo do texto que chega no webhook.
    """
    import urllib.parse

    phone_digits = re.sub(r"\D", "", phone_e164)
    text = f"{prefilled_text} #{tracking_code}"
    return f"https://wa.me/{phone_digits}?text={urllib.parse.quote(text)}"


def extract_tracking_code(message_body: str) -> str | None:
    match = TRACKING_TAG_RE.search(message_body or "")
    return match.group(1) if match else None


def attribution_from_click_bridge(db: Session, lead: Lead, message_body: str) -> UtmAttribution | None:
    code = extract_tracking_code(message_body)
    if not code:
        return None
    bridge = db.query(ClickBridge).filter(ClickBridge.tracking_code == code).first()
    if not bridge or bridge.consumed_at:
        return None

    attribution = UtmAttribution(
        lead_id=lead.id,
        utm_source=bridge.utm_source,
        utm_medium=bridge.utm_medium,
        utm_campaign=bridge.utm_campaign,
        utm_content=bridge.utm_content,
        utm_term=bridge.utm_term,
        gclid=bridge.gclid,
        tracking_code=code,
    )
    import datetime

    bridge.consumed_at = datetime.datetime.utcnow()
    db.add(bridge)
    db.add(attribution)
    db.commit()
    db.refresh(attribution)
    return attribution


def attribution_from_ctwa_referral(db: Session, lead: Lead, referral: dict) -> UtmAttribution:
    """`referral` é o objeto que a Cloud API manda no webhook quando o lead
    veio de um anúncio "Clique para WhatsApp" da Meta. Formato típico:
    {"source_id": "<ad_id>", "source_url": "...", "headline": "...",
     "ctwa_clid": "..."}
    """
    ad_id = referral.get("source_id", "")
    ad_names = fetch_ad_names(ad_id)

    attribution = UtmAttribution(
        lead_id=lead.id,
        ctwa_clid=referral.get("ctwa_clid", ""),
        ad_id=ad_id,
        ad_source_url=referral.get("source_url", ""),
        ad_headline=referral.get("headline", ""),
        utm_source="meta_ctwa",
        utm_medium="whatsapp_ad",
        utm_campaign=ad_names["campaign_name"],
        utm_content=ad_names["ad_name"],
        utm_term=ad_names["adset_name"],
    )
    db.add(attribution)
    db.commit()
    db.refresh(attribution)
    return attribution


def attribution_from_site_form(db: Session, lead: Lead, form_data: dict) -> UtmAttribution:
    attribution = UtmAttribution(
        lead_id=lead.id,
        utm_source=form_data.get("utm_source", ""),
        utm_medium=form_data.get("utm_medium", ""),
        utm_campaign=form_data.get("utm_campaign", ""),
        utm_content=form_data.get("utm_content", ""),
        utm_term=form_data.get("utm_term", ""),
        gclid=form_data.get("gclid", ""),
        fbclid=form_data.get("fbclid", ""),
        fbc=form_data.get("fbc", ""),
        fbp=form_data.get("fbp", ""),
    )
    db.add(attribution)
    db.commit()
    db.refresh(attribution)
    return attribution
