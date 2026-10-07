"""Procura nas conversas dos últimos dias os negócios que se encaixam nas sugestões de etapa
(💡 qualificado, venda fechada sem comprovante, perdido com motivo, não é venda).

Rodar no terminal do VPS (Web console da Hostinger):
    docker exec -it crm-app python -m scripts.sugestoes            # últimos 7 dias, só mostra
    docker exec -it crm-app python -m scripts.sugestoes 15         # últimos 15 dias
    docker exec -it crm-app python -m scripts.sugestoes 7 --aplicar
        # e deixa a sugestão pro vendedor confirmar no topo de cada conversa

Nada muda de etapa sozinho: com --aplicar só aparece o aviso pro vendedor confirmar.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message
from app.services import suggestions
from app.timeutil import to_local


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 7
    apply = "--aplicar" in sys.argv
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    db = SessionLocal()
    try:
        leads = (
            db.query(Lead)
            .join(Conversation, Conversation.lead_id == Lead.id)
            .filter(Conversation.last_message_at >= since, Lead.deleted_at.is_(None), Lead.is_group.is_(False))
            .distinct()
            .all()
        )
        counts: dict = {}
        print(f"\nOlhando {len(leads)} conversa(s) com mensagem nos últimos {days} dia(s)...\n")
        for lead in leads:
            before = (lead.suggest_kind, lead.suggest_text, lead.suggest_loss_reason)
            lead.suggest_kind = ""
            msgs = (
                db.query(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .filter(Conversation.lead_id == lead.id, Message.created_at >= since, Message.direction.in_(["in", "out"]))
                .order_by(Message.created_at)
                .all()
            )
            last_hit = None
            for m in msgs:
                kind_before = lead.suggest_kind
                suggestions.on_message(lead, m.body or "", outbound=m.direction == "out")
                if lead.suggest_kind and (lead.suggest_kind != kind_before or lead.suggest_text):
                    last_hit = m
            kind = suggestions.current(lead)
            if not kind:
                lead.suggest_kind, lead.suggest_text, lead.suggest_loss_reason = before
                continue
            counts[kind] = counts.get(kind, 0) + 1
            when = to_local(last_hit.created_at).strftime("%d/%m %H:%M") if last_hit else ""
            stage = lead.stage.name if lead.stage else "—"
            seller = lead.assigned_user.name if lead.assigned_user else "sem vendedor"
            extra = f" · motivo: {lead.suggest_loss_reason}" if lead.suggest_loss_reason else ""
            print(f"  💡 {suggestions.KIND_LABEL[kind]}{extra}\n     {lead.name or lead.phone} · {stage} · {seller} · {when}\n     {lead.suggest_text}\n")
            if not apply:
                lead.suggest_kind, lead.suggest_text, lead.suggest_loss_reason = before
        if apply:
            db.commit()
        else:
            db.rollback()
        summary = ", ".join(f"{suggestions.KIND_LABEL[k].lower()}: {n}" for k, n in counts.items()) or "nenhuma"
        print(f"Sugestões: {summary}")
        if counts and not apply:
            print("Pra deixar essas sugestões pro vendedor confirmar, rode de novo com --aplicar no final.")
        elif counts:
            print("Pronto: os vendedores veem o 💡 no topo de cada conversa pra confirmar ou descartar.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
