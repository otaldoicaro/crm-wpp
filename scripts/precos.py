"""Aplica nas conversas antigas as regras de preço do funil:
- o time mandou um valor (R$ 250, 250$, 15mil, 350 reais...) -> Negociando (guarda o valor);
- o cliente perguntou o preço ("valor?", "quanto custa?") -> Qualificado.
Só avança leads em aberto que ainda não passaram dessas etapas; nunca volta ninguém.

Rodar no terminal do VPS:
    docker exec -it crm-app python -m scripts.precos              # últimos 30 dias, só mostra
    docker exec -it crm-app python -m scripts.precos 60           # últimos 60 dias
    docker exec -it crm-app python -m scripts.precos 30 --aplicar # move os leads
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message
from app.services import followup, funnel


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 30
    apply = "--aplicar" in sys.argv
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (
            db.query(Message, Lead)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Lead, Lead.id == Conversation.lead_id)
            .filter(Message.created_at >= since, Message.direction.in_(["in", "out"]), Message.body != "",
                    Lead.is_group.is_(False), Lead.deleted_at.is_(None))
            .order_by(Message.created_at)
            .all()
        )
        moves = {}  # lead_id -> (lead, etapa nova, motivo, etapa de antes)
        for message, lead in rows:
            stage = lead.stage
            if stage is None or stage.is_won or stage.is_lost:
                continue
            stages = funnel._stages_cached(lead)
            if message.direction == "out":
                value = followup.find_price(message.body)
                target = funnel.stage_named(stages, funnel.NEGOTIATION_NAME, "Negociação", "Negociacao", "Oportunidade")
                if value is None or target is None:
                    continue
                why = f"time mandou valor: “{message.body.strip()[:70]}”"
            else:
                if not followup.asks_price(message.body):
                    continue
                target = funnel.stage_named(stages, "Qualificado", "Qualificados")
                if target is None:
                    continue
                why = f"cliente perguntou o preço: “{message.body.strip()[:70]}”"
            before = moves[lead.id][3] if lead.id in moves else stage.name
            current = moves[lead.id][1] if lead.id in moves else stage
            if target.order > current.order:
                moves[lead.id] = (lead, target, why, before)
                if apply:
                    if message.direction == "out":
                        followup.on_outbound_text(db, lead, message.body)
                    else:
                        followup.on_inbound_text(db, lead, message.body)
        print(f"\n{len(rows)} mensagens dos últimos {days} dias conferidas.\n")
        for lead, target, why, before in moves.values():
            print(f"  {lead.name or lead.phone}: {before} -> {target.name} · {why}")
        print(f"\nLeads que {'foram movidos' if apply else 'seriam movidos'}: {len(moves)}")
        if apply:
            db.commit()
        elif moves:
            print("Pra mover, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
