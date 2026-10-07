"""Confere a leitura de comprovantes nas imagens/PDFs que os clientes mandaram nos últimos dias.

Rodar no terminal do VPS (Web console da Hostinger):
    docker exec -it crm-app python -m scripts.comprovantes            # últimos 3 dias, só mostra
    docker exec -it crm-app python -m scripts.comprovantes 7          # últimos 7 dias
    docker exec -it crm-app python -m scripts.comprovantes 3 --aplicar
        # e cria o aviso "confirmar venda" nos negócios em aberto em que achou comprovante

Mostra, pra cada arquivo: cliente, quando chegou, etapa e o que o leitor achou. Nada é
enviado pra fora do servidor.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, WhatsAppNumber
from app.services import media_store, messaging, receipt_text, receipts
from app.timeutil import to_local


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 3
    apply = "--aplicar" in sys.argv
    if not receipt_text.ocr_available():
        print("⚠️  O leitor de imagens (Tesseract) não está instalado: rode o instalador do CRM primeiro.")
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (
            db.query(Message, Conversation, Lead)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Lead, Lead.id == Conversation.lead_id)
            .filter(Message.direction == "in", Message.media_type.in_(["image", "document"]),
                    Message.created_at >= since, Lead.is_group.is_(False), Lead.deleted_at.is_(None))
            .order_by(Message.created_at)
            .all()
        )
        print(f"\n{len(rows)} foto(s)/PDF(s) de clientes nos últimos {days} dia(s):\n")
        found = 0
        for message, conversation, lead in rows:
            content, mime = media_store.load(message.media_stored_key)
            if content is None:
                number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
                content, mime = messaging.fetch_media(number, message.media_id)
            when = to_local(message.created_at).strftime("%d/%m %H:%M")
            stage = lead.stage.name if lead.stage else "—"
            head = f"{when} · {lead.name or lead.phone} · {stage}"
            if not content:
                print(f"  {head}\n     ⚠️  arquivo não disponível (expirou no WhatsApp)\n")
                continue
            text = receipt_text.extract_text(content, mime)
            result = receipt_text.classify(text) if text.strip() else None
            if not result:
                print(f"  {head}\n     sem texto legível ({mime})\n")
                continue
            if result["is_payment_receipt"]:
                found += 1
                value = f"R$ {result['amount']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                print(f"  {head}\n     ✅ COMPROVANTE · {value} · {receipts.describe(result) or '-'} · confiança {result['confidence']}")
                if apply and receipts.worth_checking(lead) and lead.receipt_status != "confirmed":
                    lead.receipt_message_id, lead.receipt_amount = message.id, float(result["amount"])
                    lead.receipt_info, lead.receipt_status = receipts.describe(result), "pending"
                    db.commit()
                    print("     → aviso de confirmar venda criado na conversa")
                elif apply:
                    print("     (negócio já fechado ou ainda não atendido: sem aviso)")
                print()
            else:
                print(f"  {head}\n     não é comprovante\n")
        print(f"Comprovantes encontrados: {found}")
        if found and not apply:
            print("Pra criar o aviso de confirmar venda nesses negócios, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
