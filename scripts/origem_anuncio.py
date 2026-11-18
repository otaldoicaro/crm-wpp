"""📣 Confere a origem das vendas e recupera a de anúncio que se perdeu.

Lead que clicou num anúncio "Clique para WhatsApp" e entrou pela busca de mensagens perdidas (sessão
"surda", reconexão pelo QR) ficava sem origem: aparecia como "Direto" e a venda não contava pro
tráfego. Este script, pra um cliente:
1. mostra as vendas fechadas no período por origem (Meta, Google, Orgânico, Direto...);
2. pra cada lead do período SEM origem, procura no Evolution a 1ª mensagem dele: se veio de anúncio,
   mostra (e com --aplicar grava a origem do anúncio no lead).

No terminal do VPS (o 1º argumento é o identificador do cliente no CRM):
    docker exec -i crm-app python -m scripts.origem_anuncio novaviseu 30            # só mostra
    docker exec -i crm-app python -m scripts.origem_anuncio novaviseu 30 --aplicar  # grava a origem
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from sqlalchemy import func

from app.db import SessionLocal
from app.models import Lead, LidMap, PipelineStage, Tenant, WhatsAppNumber
from app.services import attribution, evolution_client
from app.services.inbox_state import NOT_SALES_TAGS
from app.services.platform import PLATFORM_LABEL, resolve_platform


def _first_ad(instance: str, jids: list):
    """Anúncio da conversa (o externalAdReply de alguma mensagem do cliente) ou None."""
    from app.routers.webhooks_evolution import _parse_content, _referral_from_context

    for jid in jids:
        try:
            records = evolution_client.chat_messages(instance, jid, limit=50)
        except evolution_client.EvolutionError:
            continue
        for rec in sorted(records, key=lambda r: int(r.get("messageTimestamp") or 0)):
            if (rec.get("key") or {}).get("fromMe"):
                continue
            _, _, context_info = _parse_content(rec.get("message") or {})
            referral = _referral_from_context(rec.get("contextInfo") or context_info or {})
            if referral:
                return referral
    return None


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--aplicar" in sys.argv
    if not args:
        print("use: python -m scripts.origem_anuncio <identificador do cliente> [dias] [--aplicar]")
        return
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.subdomain == args[0]).first()
        if tenant is None:
            print(f"cliente '{args[0]}' não encontrado")
            return
        since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        won_ids = [s.id for s in db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id, PipelineStage.is_won.is_(True))]
        base = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None), Lead.is_group.is_(False),
                                     Lead.tag.notin_(NOT_SALES_TAGS))

        def show_sales(title):
            closed = func.coalesce(Lead.closed_at, Lead.updated_at, Lead.created_at)
            sales = base.filter(Lead.stage_id.in_(won_ids), closed >= since).all()
            by: dict = {}
            for lead in sales:
                key = resolve_platform(lead.attribution)
                row = by.setdefault(key, [0, 0.0, 0])
                row[0] += 1
                row[1] += lead.deal_value or 0
                row[2] += 1 if lead.created_at >= since else 0
            print(f"\n== {title}: vendas fechadas nos últimos {days} dias: {len(sales)} | "
                  f"R$ {sum(l.deal_value or 0 for l in sales):,.0f}".replace(",", "."))
            for key, (n, value, new) in sorted(by.items(), key=lambda kv: -kv[1][1]):
                print(f"   {PLATFORM_LABEL.get(key, key):10} {n:4} vendas | R$ {value:>12,.0f} | {new} de leads que entraram no período".replace(",", "."))

        show_sales("ANTES")
        numbers = {n.id: n for n in db.query(WhatsAppNumber).filter(WhatsAppNumber.tenant_id == tenant.id,
                                                                     WhatsAppNumber.provider == "evolution")}
        lids_by_phone: dict = {}
        for lid, phone in db.query(LidMap.lid, LidMap.phone):
            lids_by_phone.setdefault(phone, []).append(lid)
        pending = [l for l in base.filter(Lead.created_at >= since) if attribution.is_empty(l.attribution)
                   and l.phone and l.phone.isdigit() and l.whatsapp_number_id in numbers]
        print(f"\nleads do período sem origem (WhatsApp): {len(pending)} — procurando anúncio no Evolution…")
        found = sold = 0
        for lead in pending:
            number = numbers[lead.whatsapp_number_id]
            jids = [lead.phone + "@s.whatsapp.net"] + [lid + "@lid" for lid in lids_by_phone.get(lead.phone, [])]
            referral = _first_ad(number.evolution_instance, jids)
            if not referral:
                continue
            found += 1
            won = lead.stage_id in won_ids
            sold += 1 if won else 0
            print(f"   {'💰 ' if won else ''}{lead.name or lead.phone} | anúncio: {(referral.get('headline') or referral.get('source_id') or '?')[:50]}")
            if apply:
                attribution.attribution_from_ctwa_referral(db, lead, referral)
        print(f"\ncom anúncio encontrado: {found} (vendas entre eles: {sold})")
        if apply and found:
            db.expire_all()
            show_sales("DEPOIS")
        elif found:
            print("rode de novo com --aplicar pra gravar a origem desses leads")
    finally:
        db.close()


if __name__ == "__main__":
    main()
