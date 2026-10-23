"""🔥 Vasculha as conversas atrás de sinal de compra DEPOIS do preço ("como faço?", "como funciona?",
"qual o pix?", "faz entrega?"...).

1) Nas vendas (Ganho) do período: quantas tiveram esse sinal e quais frases mais aparecem, e as
   mensagens curtas mais comuns que o cliente mandou depois do preço e que a lista ainda não pega
   (pra aumentar a lista em app/services/buying.py).
2) Nas negociações em aberto que já receberam preço: quem mandou sinal e ficou sem fechar. Com
   --aplicar, esses leads ganham a etiqueta "🔥 quis comprar depois do preço" e entram no filtro do
   Inbox (💡 Avisos → 🔥 Quis comprar depois do preço) pro vendedor fazer o follow-up.

    docker exec -it crm-app python -m scripts.sinais_compra --cliente novaviseu
    docker exec -it crm-app python -m scripts.sinais_compra --cliente novaviseu --aplicar
(--dias 60 = período; padrão 60)
"""

import datetime
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, PipelineStage, Tenant
from app.services import buying, followup


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def after_price(db, lead_ids: list) -> dict:
    """{lead_id: [(quando, texto do cliente)] depois do 1º preço que o time mandou}."""
    msgs = defaultdict(list)
    rows = (db.query(Conversation.lead_id, Message.direction, Message.body, Message.created_at)
            .join(Message, Message.conversation_id == Conversation.id)
            .filter(Conversation.lead_id.in_(lead_ids), Message.direction.in_(["in", "out"]))
            .order_by(Message.created_at).all())
    priced = set()
    for lead_id, direction, body, when in rows:
        if direction == "out" and lead_id not in priced and followup.find_prices(body or ""):
            priced.add(lead_id)
        elif direction == "in" and lead_id in priced and body:
            msgs[lead_id].append((when, body))
    return msgs


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    key = _arg("--cliente")
    days = int(_arg("--dias", "60"))
    apply_changes = "--aplicar" in sys.argv
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).all()
        won = [s.id for s in stages if s.is_won]
        closed = [s.id for s in stages if s.is_won or s.is_lost]
        base = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.deleted_at.is_(None), Lead.is_group.is_(False),
                                     Lead.created_at >= since)

        # 1) vendas
        sales = base.filter(Lead.stage_id.in_(won)).all()
        msgs = after_price(db, [l.id for l in sales]) if sales else {}
        hits, phrases, loose = 0, Counter(), Counter()
        for lead in sales:
            found = set()
            for when, body in msgs.get(lead.id, []):
                if lead.closed_at and when > lead.closed_at:
                    break
                phrase = buying.detect(body)
                if phrase:
                    found.add(phrase)
                elif len(body.split()) <= 6:
                    loose[buying._plain(body).strip(" ?!.")] += 1
            hits += bool(found)
            phrases.update(found)
        print(f"\n{tenant.name} · últimos {days} dias\n")
        print(f"1) Vendas: {hits} de {len(sales)} ({round(100 * hits / len(sales)) if sales else 0}%) tiveram sinal de compra depois do preço.")
        for phrase, n in phrases.most_common(15):
            print(f"   {n:4d} · “{phrase}”")
        print("\n   Mensagens curtas mais comuns depois do preço (que a lista ainda não pega):")
        for text, n in [(t, n) for t, n in loose.most_common(40) if t and len(t) > 2][:25]:
            print(f"   {n:4d} · “{text}”")

        # 2) negociações em aberto
        open_leads = base.filter(~Lead.stage_id.in_(closed), Lead.interest_at.is_(None)).all()
        msgs = after_price(db, [l.id for l in open_leads]) if open_leads else {}
        marked, by_seller = [], Counter()
        for lead in open_leads:
            for when, body in reversed(msgs.get(lead.id, [])):  # o sinal mais recente
                if buying.detect(body):
                    marked.append((lead, when, body))
                    by_seller[lead.assigned_user.name if lead.assigned_user else "sem vendedor"] += 1
                    break
        print(f"\n2) Em aberto: {len(marked)} lead(s) receberam preço, mostraram interesse e não fecharam.")
        for name, n in by_seller.most_common():
            print(f"   {n:4d} · {name}")
        for lead, when, body in marked[:10]:
            print(f"   · {lead.name or lead.phone}: “{body.strip()[:70]}”")
        if apply_changes:
            for lead, when, body in marked:
                lead.interest_at, lead.interest_text = when, body.strip().replace("\n", " ")[:255]
            db.commit()
            print(f"\n✅ {len(marked)} marcados: Inbox → 💡 Avisos → 🔥 Quis comprar depois do preço.")
        elif marked:
            print("\nNada foi marcado. Pra colocar a etiqueta 🔥 neles, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
