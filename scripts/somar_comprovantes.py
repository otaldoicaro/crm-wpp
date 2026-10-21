"""💸 Corrige o valor das vendas (Ganho) em que o cliente pagou com mais de um comprovante.

Antes, só o 1º comprovante contava: quem comprou 2 itens e pagou cada um separado ficou com a venda
pela metade. Agora o CRM soma sozinho os próximos; este script arruma as vendas que já fecharam.

Pra cada venda de um cliente (--cliente) fechada nos últimos N dias (--dias, padrão 30):
lê as fotos/PDFs que o cliente mandou (só as cópias que o CRM já guardou) e soma os comprovantes
diferentes (o mesmo comprovante mandado 2x conta uma vez). Se der mais que o valor da venda,
mostra "de R$ X pra R$ Y". Venda de 1 comprovante só, ou que já está certa, fica como está.

É pesado (lê imagens): rodar à noite. Sem --aplicar só mostra.
    docker exec -it crm-app nice -n 19 python -m scripts.somar_comprovantes --cliente novaviseu
    docker exec -d crm-app sh -c 'nice -n 19 python -m scripts.somar_comprovantes --cliente novaviseu --aplicar > /tmp/somar.log 2>&1'
    docker exec crm-app tail -n 20 /tmp/somar.log
"""

import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, PipelineStage, Tenant
from app.services import media_store, receipt_text, receipts

MAX_MEDIA = 12  # fotos/PDFs lidos por venda (comprovante costuma vir perto do fechamento)


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def found_receipts(db, lead: Lead) -> list:
    """[(valor, descrição, certeza)] dos comprovantes diferentes que o cliente mandou nesse negócio."""
    msgs = (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id == lead.id, Message.direction == "in", Message.media_type.in_(["image", "document"]),
                Message.media_stored_key != "", Message.created_at >= lead.created_at - datetime.timedelta(minutes=1))
        .order_by(Message.created_at.desc())
        .limit(MAX_MEDIA)
        .all()
    )
    quoted = receipts._quoted_prices(db, lead) + ([lead.deal_value] if lead.deal_value else [])
    out = []
    for m in msgs:
        content, mime = media_store.load(m.media_stored_key)
        if not content:
            continue
        result = receipt_text.read_receipt(content, mime, quoted)
        amount = float(result.get("amount") or 0)
        if not result.get("is_payment_receipt") or result.get("confidence") == "baixa" or amount <= 0:
            continue
        info = receipts.describe(result)
        if any(abs(v - amount) < 0.01 and (i == info or not i or not info) for v, i, _ in out):
            continue  # o mesmo comprovante mandado de novo
        out.append((amount, info, result.get("confidence")))
    return out


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    key = _arg("--cliente")
    days = int(_arg("--dias", "30"))
    apply_changes = "--aplicar" in sys.argv
    if not receipt_text.ocr_available():
        print("⚠️ Leitor de imagens (Tesseract) não está instalado no servidor.")
        return
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        won = [s.id for s in db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id, PipelineStage.is_won.is_(True))]
        since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        ids = [lid for (lid,) in db.query(Lead.id).filter(
            Lead.tenant_id == tenant.id, Lead.stage_id.in_(won), Lead.closed_at >= since, Lead.deleted_at.is_(None))]
        print(f"\n{tenant.name}: conferindo {len(ids)} venda(s) dos últimos {days} dias...\n")
        changed, extra = 0, 0.0
        for lead_id in ids:
            lead = db.get(Lead, lead_id)
            found = found_receipts(db, lead)
            time.sleep(0.2)  # respiro: o CRM continua atendendo
            if len(found) < 2:
                continue
            total = round(sum(v for v, _, _ in found), 2)
            if total <= (lead.deal_value or 0) + 0.01:
                continue
            parts = " + ".join(f"R$ {receipts._brl(v)}" + ("" if c == "alta" else " (?)") for v, _, c in found)
            print(f"• {lead.name or lead.phone}: R$ {receipts._brl(lead.deal_value or 0)} -> R$ {receipts._brl(total)}  ({parts})")
            changed += 1
            extra += total - (lead.deal_value or 0)
            if apply_changes:
                old = lead.deal_value or 0
                lead.deal_value = total
                lead.receipt_log = "\n".join(f"{v:.2f}|{i}" for v, i, _ in found)
                conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
                if conv is not None:
                    db.add(Message(conversation_id=conv.id, direction="note",
                                   body=f"💸 Venda corrigida de R$ {receipts._brl(old)} pra R$ {receipts._brl(total)}: o cliente "
                                        f"pagou com {len(found)} comprovantes ({parts}). Se não era tudo da mesma compra, é só corrigir o valor."))
                db.commit()
        print(f"\nResumo: {changed} venda(s) com mais de um comprovante · + R$ {receipts._brl(extra)} no faturamento.")
        print("(?) = valor lido sem 100% de certeza: confira na conversa.")
        if changed and not apply_changes:
            print("Nada foi mudado. Pra aplicar, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
