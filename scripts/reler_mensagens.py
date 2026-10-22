"""Relê no Evolution as mensagens que ficaram como "📎 Mensagem que o CRM ainda não mostra" e
troca pelo conteúdo, agora que o CRM entende mais tipos (enquete, vídeo bolinha, figurinha
animada, resposta de botão, convite de grupo, ligação...).

Rodar no terminal do VPS (Web console da Hostinger):
    docker exec -it crm-app python -m scripts.reler_mensagens             # só mostra o que mudaria
    docker exec -it crm-app python -m scripts.reler_mensagens --aplicar   # corrige

Também lista os tipos que o CRM ainda não conhece (pra ensinar depois).
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Message, WhatsAppNumber
from app.routers.webhooks_evolution import IGNORED_TYPES, UNSUPPORTED_TEXT, _parse_content
from app.services import evolution_client


def main() -> None:
    apply = "--aplicar" in sys.argv
    db = SessionLocal()
    fixed, removed, unknown, missing = 0, 0, Counter(), 0
    try:
        rows = (
            db.query(Message, WhatsAppNumber)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(WhatsAppNumber, WhatsAppNumber.id == Conversation.whatsapp_number_id)
            .filter(Message.body == UNSUPPORTED_TEXT, Message.wa_message_id != "")
            .all()
        )
        print(f"\n{len(rows)} mensagem(ns) com o aviso. {'Corrigindo' if apply else 'Conferindo'}...\n")
        for msg, number in rows:
            if not number.evolution_instance:
                missing += 1
                continue
            try:
                rec = evolution_client.find_message(number.evolution_instance, msg.wa_message_id)
            except Exception:  # noqa: BLE001 — número desconectado/apagado no Evolution
                rec = {}
            content = rec.get("message") or {}
            if not content:
                missing += 1
                continue
            body, media_type, _ = _parse_content(content)
            if body == UNSUPPORTED_TEXT:
                unknown.update(k for k in content if k not in IGNORED_TYPES)
                continue
            if not body and not media_type:
                removed += 1  # era só metadado (ex: aviso de álbum): não é mensagem
                if apply:
                    db.delete(msg)
                continue
            fixed += 1
            print(f"  ✓ {msg.created_at:%d/%m %H:%M}  {body[:90]}")
            if apply:
                msg.body, msg.media_type = body, media_type
                if media_type:
                    msg.media_id = msg.wa_message_id  # a mídia é buscada pelo id da mensagem
        if apply:
            db.commit()
    finally:
        db.close()
    print(f"\n{'Corrigidas' if apply else 'Dá pra corrigir'}: {fixed} · {'apagadas' if apply else 'a apagar'} (não eram mensagem): {removed}"
          f" · não achadas no Evolution: {missing}")
    if unknown:
        print("Tipos que o CRM ainda não conhece (me mande esta linha): " + ", ".join(f"{k} ({n})" for k, n in unknown.most_common()))
    if not apply and (fixed or removed):
        print("\nPra corrigir de verdade, rode de novo com --aplicar no final.")


if __name__ == "__main__":
    main()
