"""💸 Confirma de uma vez os comprovantes antigos que ficaram "a confirmar" (o CRM leu o valor, mas
ninguém clicou em confirmar). Daqui pra frente, os novos aparecem como notificação pro vendedor.

Mandou comprovante = venda. Pra cada comprovante pendente de UM cliente (--cliente):
- negócio em aberto ou em Perdido (ex: foi pra Perdido por ficar parado) -> Ganho com o valor do comprovante;
- já em Ganho -> o valor da venda fica o do comprovante (mostra quando muda); se a venda já tinha
  outro comprovante, este é de mais um item: soma.
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
            extra = bool(stage is not None and stage.is_won and receipts.logged(lead)
                         and not receipts.already_counted(lead, amount, lead.receipt_info))
            if extra:  # a venda já tinha outro comprovante: este é de mais um item, soma
                old = lead.deal_value or 0
                action = f"mais um comprovante: Ganho R$ {receipts._brl(old)} + R$ {receipts._brl(amount)}"
                total += amount
            elif stage is not None and stage.is_won:
                old = lead.deal_value or 0
                action = "confirma o Ganho" if abs(old - amount) < 0.01 else (
                    f"Ganho: R$ {receipts._brl(old)} -> R$ {receipts._brl(amount)}" if old else f"Ganho fica com R$ {receipts._brl(amount)}")
                total += amount - old
            else:
                was = f"{stage.name if stage else '—'}" + (f" ({lead.loss_reason or 'sem motivo'}, {lead.loss_detail or 'marcado à mão'})" if stage and stage.is_lost else "")
                action = f"{was} -> Ganho · R$ {receipts._brl(amount)}"
                total += amount
            print(f"• {name}: {action} ({lead.receipt_info})")
            if apply_changes:
                if extra:
                    lead.deal_value = round((lead.deal_value or 0) + amount, 2)
                    lead.receipt_status = "confirmed"
                    receipts.log_receipt(lead, amount, lead.receipt_info)
                    _note(db, lead, f"💸 Mais um comprovante de R$ {receipts._brl(amount)} somado à venda: agora R$ {receipts._brl(lead.deal_value)}")
                elif stage is not None and stage.is_won:
                    if abs((lead.deal_value or 0) - amount) > 0.01:
                        _note(db, lead, f"💸 Valor da venda ajustado pro do comprovante: R$ {receipts._brl(lead.deal_value or 0)} -> R$ {receipts._brl(amount)}")
                    lead.deal_value = amount
                    lead.receipt_status = "confirmed"
                    receipts.log_receipt(lead, amount, lead.receipt_info)
                elif won is not None:
                    receipts.close_as_sale(db, lead, amount, lead.receipt_info, True,
                                           "comprovante antigo (confirmação em massa: mandou comprovante = venda)")
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
