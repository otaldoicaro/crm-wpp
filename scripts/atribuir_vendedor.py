"""👤 Passa todas as conversas de um cliente pro dono do WhatsApp conectado (ou pro vendedor que você
disser). Pra quando o cliente tem um vendedor só: os leads sem vendedor (ou com outro) ficam com ele, e o
WhatsApp conectado que está como "compartilhado" passa a ser dele (os leads novos já chegam com ele).

Pula grupo e lead na Lixeira. Não manda mensagem pra ninguém. Sem --aplicar só mostra.

    docker exec -it crm-app python -m scripts.atribuir_vendedor --cliente junta
    docker exec -it crm-app python -m scripts.atribuir_vendedor --cliente junta --aplicar
    (com mais de um vendedor: --vendedor email@do.vendedor)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Lead, Tenant, User
from app.services import handover, ownership


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def main() -> None:
    key, email = _arg("--cliente"), _arg("--vendedor").strip().lower()
    apply_changes = "--aplicar" in sys.argv
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        numbers = [n for n in tenant.whatsapp_numbers if handover.usable(n)]
        people = db.query(User).filter(User.tenant_id == tenant.id, User.is_active.is_(True), User.read_only.is_(False)).all()
        if email:
            target = next((u for u in people if u.email.lower() == email), None)
        else:  # o dono do WhatsApp conectado; sem dono, o único vendedor (não admin) do cliente
            owners = {n.owner_user_id for n in numbers if n.owner_user_id}
            sellers = [u for u in people if u.role != "admin"]
            target = (next((u for u in people if u.id in owners), None) if len(owners) == 1
                      else sellers[0] if not owners and len(sellers) == 1 else None)
        if target is None:
            print("⚠️ Não sei pra quem passar. Diga o vendedor: --vendedor <e-mail>. Pessoas ativas:")
            for u in people:
                print(f"   - {u.name} <{u.email}> ({u.role})")
            return
        print(f"Cliente: {tenant.name} · vendedor: {target.name} <{target.email}>")
        for n in numbers:
            if not n.owner_user_id:
                print(f"📱 WhatsApp {n.label} ({n.phone_number or 'sem número'}): compartilhado → passa a ser de {target.name}")
                if apply_changes:
                    n.owner_user_id = target.id
        leads = db.query(Lead).filter(
            Lead.tenant_id == tenant.id, Lead.is_group.is_(False), Lead.deleted_at.is_(None),
            (Lead.assigned_user_id != target.id) | Lead.assigned_user_id.is_(None),
        ).all()
        for lead in leads:
            before = lead.assigned_user.name if lead.assigned_user else "sem vendedor"
            print(f"   {lead.name or lead.phone}: {before} → {target.name}")
            if apply_changes:
                previous = lead.assigned_user_id
                lead.assigned_user_id = target.id
                for conversation in lead.conversations:
                    conversation.assigned_user_id = target.id
                ownership.changed(db, lead, previous, "todas as conversas pro vendedor do WhatsApp (script)", note=False)
        print(f"\n{len(leads)} lead(s) {'passados' if apply_changes else 'pra passar'} pra {target.name}.")
        if apply_changes:
            db.commit()
            print("✅ Pronto.")
        else:
            print("Nada foi alterado. Pra fazer de verdade, rode de novo com --aplicar.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
