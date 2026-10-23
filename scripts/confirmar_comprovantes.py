"""💸 Confirma de uma vez os comprovantes antigos que ficaram "a confirmar" (o CRM leu o valor, mas
ninguém clicou em confirmar). Daqui pra frente, os novos aparecem como notificação pro vendedor.

Pra cada comprovante pendente de UM cliente (--cliente):
- negócio em aberto -> Ganho com o valor lido do comprovante;
- já em Ganho sem valor -> põe o valor do comprovante; já com o mesmo valor -> só confirma;
- já em Ganho com outro valor, ou em Perdido -> não mexe: lista pra você conferir na conversa.
Não manda evento de venda pro Meta (venda antiga ensinaria o algoritmo errado).
Sem --aplicar só mostra.

    docker exec -it crm-app python -m scripts.confirmar_comprovantes --cliente novaviseu
    docker exec -it crm-app python -m scripts.confirmar_comprovantes --cliente novaviseu --aplicar
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, Tenant
from app.services import autostage, funnel, receipts


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def _note(db, lead: Lead, text: str) -> None:
    conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
    if conv is not None:
        db.add(Message(conversation_id=conv.id, direction="note", body=text))


def main() -> None:
    key = _arg("--cliente")
    apply_changes = "--aplicar" in sys.argv
    autostage.QUIET = True  # sem evento de venda pro Meta
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        won = next((s for s in funnel._stages(db, tenant.id) if s.is_won), None)
        leads = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.receipt_status == "pending", Lead.deleted_at.is_(None)).all()
        print(f"\n{tenant.name}: {len(leads)} comprovante(s) a confirmar.\n")
        total, review = 0.0, []
        for lead in leads:
            amount = lead.receipt_amount or 0
            stage = lead.stage
            name = lead.name or lead.phone
            if amount <= 0:
                review.append(f"{name}: comprovante sem valor lido")
                continue
            if stage is not None and stage.is_lost:
                review.append(f"{name}: está em Perdido, mas mandou comprovante de R$ {receipts._brl(amount)}")
                continue
            if stage is not None and stage.is_won:
                if lead.deal_value and abs(lead.deal_value - amount) > 0.01:
                    review.append(f"{name}: Ganho de R$ {receipts._brl(lead.deal_value)}, comprovante de R$ {receipts._brl(amount)}")
                    continue
                action = "confirma o Ganho" if lead.deal_value else f"Ganho fica com R$ {receipts._brl(amount)}"
            else:
                action = f"{stage.name if stage else '—'} -> Ganho · R$ {receipts._brl(amount)}"
            print(f"• {name}: {action} ({lead.receipt_info})")
            total += amount if not lead.deal_value else 0
            if apply_changes:
                if won is not None and not (stage and stage.is_won):
                    funnel.set_stage(lead, won)
                if not lead.deal_value:
                    lead.deal_value = amount
                lead.receipt_status = "confirmed"
                receipts.log_receipt(lead, amount, lead.receipt_info)
                _note(db, lead, f"💸 Venda confirmada pelo comprovante (R$ {receipts._brl(amount)}) — confirmação em massa dos comprovantes antigos")
                db.commit()
        if review:
            print("\nPra conferir na conversa (não mexi):")
            for line in review:
                print(f"  ? {line}")
        print(f"\nResumo: + R$ {receipts._brl(total)} no faturamento.")
        if not apply_changes:
            print("Nada foi mudado. Pra confirmar, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
