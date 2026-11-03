"""Leads "fantasma": nasceram com o código LID do WhatsApp no lugar do telefone (a mensagem chegou
sem o número e o CRM não sabia de quem era). Pra cada um dos últimos dias mostra o que tem dentro e
tenta descobrir o telefone pela própria conversa no Evolution (uma mensagem dela que já está no CRM
num lead de verdade). Com --aplicar guarda o que descobriu e junta o fantasma no lead certo.

No terminal do VPS:
    docker exec -i crm-app python -m scripts.fantasmas            # só mostra
    docker exec -i crm-app python -m scripts.fantasmas --aplicar  # junta os que deu pra descobrir
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, LidMap, Message, WhatsAppNumber
from app.services.history_sync import is_lid_phone, lid_from_chat
from app.timeutil import to_local


def main() -> None:
    apply = "--aplicar" in sys.argv
    days = next((int(a) for a in sys.argv[1:] if a.isdigit()), 7)
    db = SessionLocal()
    try:
        since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        ghosts = [l for l in db.query(Lead).filter(Lead.created_at >= since, Lead.deleted_at.is_(None),
                                                   Lead.is_group.is_(False))
                  if is_lid_phone(l.phone or "") and db.get(LidMap, l.phone) is None]
        found = 0
        for lead in sorted(ghosts, key=lambda l: l.created_at):
            convs = db.query(Conversation).filter(Conversation.lead_id == lead.id).all()
            msgs = (db.query(Message).filter(Message.conversation_id.in_([c.id for c in convs]))
                    .order_by(Message.created_at).all()) if convs else []
            ins = [m for m in msgs if m.direction == "in"]
            outs = [m for m in msgs if m.direction == "out"]
            phone = ""
            for conv in convs:
                number = db.get(WhatsAppNumber, conv.whatsapp_number_id)
                if number is not None and number.provider == "evolution":
                    phone = lid_from_chat(db, number.evolution_instance, lead.phone + "@lid")
                    if phone:
                        break
            first = msgs[0] if msgs else None
            print(f"{to_local(lead.created_at):%d/%m %H:%M} | {lead.name or '-'} | {lead.assigned_user.name if lead.assigned_user else '-'}"
                  f" | {len(ins)} do cliente, {len(outs)} nossas"
                  + (f" | 1a: {to_local(first.created_at):%d/%m %H:%M} {first.direction} {(first.body or '')[:40]!r}" if first else "")
                  + f" | telefone: {phone or 'nao descobri'}")
            if phone:
                found += 1
                if apply:
                    db.add(LidMap(lid=lead.phone, phone=phone))
                    db.commit()
        print(f"\nfantasmas: {len(ghosts)} | telefone descoberto: {found}")
        if apply and found:
            from app.services.lead_match import merge_lid_leads

            done = merge_lid_leads(db)
            print(f"juntados/corrigidos: {len(done)}")
            for name, what, before, after in done:
                print(f"   {name}: {what} ({before} -> {after})")
        elif found:
            print("rode de novo com --aplicar pra juntar")
    finally:
        db.close()


if __name__ == "__main__":
    main()
