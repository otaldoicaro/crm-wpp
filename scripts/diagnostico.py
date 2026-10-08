"""Diagnóstico de uma conversa: mostra tudo o que o CRM tem de um cliente (todos os cadastros
com o mesmo telefone, negócios, lixeira, conversas por número, quantidade e datas das mensagens)
e se os WhatsApps estão conectados. Não mostra o texto das mensagens e não muda nada.

Rodar no terminal do VPS (Web console da Hostinger):
    docker exec -it crm-app python -m scripts.diagnostico 21984127467
    docker exec -it crm-app python -m scripts.diagnostico "Lima"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from sqlalchemy import func, or_

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, WhatsAppNumber
from app.services.attribution import _phone_variants, normalize_br_phone
from app.timeutil import to_local


def fmt(dt):
    return to_local(dt).strftime("%d/%m/%Y %H:%M") if dt else "—"


def main() -> None:
    if len(sys.argv) < 2:
        print("Use: python -m scripts.diagnostico <telefone ou nome>")
        return
    term = " ".join(sys.argv[1:]).strip()
    digits = "".join(ch for ch in term if ch.isdigit())
    db = SessionLocal()
    try:
        query = db.query(Lead)
        if len(digits) >= 8:
            variants = _phone_variants(digits) | _phone_variants(normalize_br_phone(digits))
            query = query.filter(or_(Lead.phone.in_(variants), Lead.phone.like(f"%{digits[-8:]}")))
        else:
            query = query.filter(Lead.name.ilike(f"%{term}%"))
        leads = query.order_by(Lead.created_at).limit(20).all()
        print(f"\n{len(leads)} cadastro(s) encontrado(s) para “{term}”:\n")
        for lead in leads:
            flags = []
            if lead.deleted_at:
                flags.append(f"NA LIXEIRA desde {fmt(lead.deleted_at)}")
            if lead.archived_at:
                flags.append(f"arquivado desde {fmt(lead.archived_at)}")
            if lead.is_group:
                flags.append("grupo")
            print(f"• {lead.name or '(sem nome)'} · {lead.phone} · criado {fmt(lead.created_at)} · origem {lead.source or '—'}")
            print(f"  etapa {lead.stage.name if lead.stage else '—'} · vendedor {lead.assigned_user.name if lead.assigned_user else 'nenhum'}"
                  f" · negócio nº {lead.deal_number or 1}{' (recompra)' if lead.contact_id else ''}"
                  + (" · " + " · ".join(flags) if flags else ""))
            convs = db.query(Conversation).filter(Conversation.lead_id == lead.id).all()
            if not convs:
                print("  sem conversa no CRM")
            for conv in convs:
                number = db.get(WhatsAppNumber, conv.whatsapp_number_id)
                rows = dict(db.query(Message.direction, func.count(Message.id)).filter(Message.conversation_id == conv.id).group_by(Message.direction).all())
                first, last = db.query(func.min(Message.created_at), func.max(Message.created_at)).filter(Message.conversation_id == conv.id).one()
                state = "conectado" if number and (number.provider != "evolution" or number.connection_state == "open") else "DESCONECTADO"
                print(f"  conversa pelo {number.label if number else '?'} ({state}): {rows.get('in', 0)} do cliente, "
                      f"{rows.get('out', 0)} nossas, {rows.get('note', 0)} avisos · de {fmt(first)} até {fmt(last)}")
            print()
        print("WhatsApps:")
        for n in db.query(WhatsAppNumber).filter(WhatsAppNumber.is_active.is_(True)).order_by(WhatsAppNumber.label):
            state = "conectado" if n.provider != "evolution" or n.connection_state == "open" else "DESCONECTADO"
            print(f"  {n.label}: {state}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
