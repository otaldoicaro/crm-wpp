"""Mensagens editadas no celular que o CRM mostrou como "✏️ (editada no celular — veja o texto
novo lá)": tenta abrir a edição de novo e trocar pelo texto novo. Também diz o motivo quando não
consegue (pra eu corrigir de vez).

Rodar no terminal do VPS (Web console da Hostinger):
    docker exec -it crm-app python -m scripts.edicoes             # só mostra
    docker exec -it crm-app python -m scripts.edicoes --aplicar   # corrige
"""

import datetime
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, WhatsAppNumber
from app.routers.webhooks_evolution import EDIT_MARK, _author_jids, _own_jids, jid_variants
from app.services import evolution_client
from app.services.msgsecret import decrypt_edit, text_from_message, to_bytes


def _edit_from_evolution(number: WhatsAppNumber, msg: Message) -> tuple:
    """(secretEncryptedMessage, item) da edição guardada no Evolution, ou ({}, {})."""
    since = msg.created_at - datetime.timedelta(minutes=1)
    until = msg.created_at + datetime.timedelta(hours=2)
    try:
        chunk = evolution_client.find_messages(number.evolution_instance, since.isoformat() + "Z", until.isoformat() + "Z", per_page=500)
    except Exception:  # noqa: BLE001
        return {}, {}
    for rec in chunk.get("records") or []:
        content = rec.get("message") or {}
        if "editedMessage" in content:
            content = (content["editedMessage"] or {}).get("message") or content
        sec = content.get("secretEncryptedMessage") or {}
        if ((sec.get("targetMessageKey") or {}).get("id")) == msg.wa_message_id:
            return sec, rec
    return {}, {}


def main() -> None:
    apply = "--aplicar" in sys.argv
    db = SessionLocal()
    reasons, fixed = Counter(), 0
    try:
        rows = (
            db.query(Message, WhatsAppNumber, Lead)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(WhatsAppNumber, WhatsAppNumber.id == Conversation.whatsapp_number_id)
            .join(Lead, Lead.id == Conversation.lead_id)
            .filter(Message.body.like(f"%{EDIT_MARK}%"))
            .all()
        )
        print(f"\n{len(rows)} mensagem(ns) marcada(s) como editada no celular.\n")
        for msg, number, lead in rows:
            if not msg.secret:
                reasons["a mensagem original não tem a chave guardada (veio antes da versão que guarda)"] += 1
                continue
            if msg.pending_edit:
                data = json.loads(msg.pending_edit)
                payload, iv, jids = data.get("payload"), data.get("iv"), list(data.get("jids") or [])
            else:
                sec, rec = _edit_from_evolution(number, msg)
                if not sec:
                    reasons["a edição não foi achada no Evolution"] += 1
                    continue
                payload, iv, jids = sec.get("encPayload"), sec.get("encIv"), _author_jids(number, rec)
            phone = (lead.phone or "").split("@")[0]
            jids += _own_jids(number) + ([f"{phone}@s.whatsapp.net"] if phone.isdigit() else [])
            jids = jid_variants(db, jids)
            plain = decrypt_edit(msg.secret, msg.wa_message_id, jids, to_bytes(payload), to_bytes(iv))
            text = text_from_message(plain) if plain else ""
            if not text:
                reasons[f"não abriu nem com {len(jids)} ids de remetente ({', '.join(j.split('@')[1] for j in jids)})"] += 1
                continue
            fixed += 1
            print(f"  ✓ {msg.created_at:%d/%m %H:%M}  {msg.body.replace(EDIT_MARK, '').strip()[:50]}  →  {text[:60]}")
            if apply:
                msg.body, msg.edited_at, msg.pending_edit = text, datetime.datetime.utcnow(), ""
        if apply:
            db.commit()
    finally:
        db.close()
    print(f"\n{'Corrigidas' if apply else 'Dá pra corrigir'}: {fixed}")
    for reason, n in reasons.most_common():
        print(f"  • {n} sem texto novo: {reason}")
    if not apply and fixed:
        print("\nPra corrigir de verdade, rode de novo com --aplicar no final.")
    if reasons:
        print("\nMe mande um print deste resultado.")


if __name__ == "__main__":
    main()
