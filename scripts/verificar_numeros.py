"""📵 Confere, SEM mandar mensagem, se os leads com mensagem que não saiu têm WhatsApp de verdade.

Pergunta pro WhatsApp (pelo Evolution) se o número existe, com e sem o 9º dígito:
- não existe de jeito nenhum -> Perdido · "Contato inválido" (com nota na conversa);
- existe só com/sem o 9 -> corrige o telefone do lead (aí a mensagem passa a sair);
- existe -> não mexe (a falha foi o WhatsApp do vendedor desconectado, não o número).
Pula grupo e lead com o código interno do WhatsApp (LID) no lugar do telefone.
Sem --aplicar só mostra.

    docker exec -it crm-app python -m scripts.verificar_numeros --cliente novaviseu
    docker exec -it crm-app python -m scripts.verificar_numeros --cliente novaviseu --aplicar
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, Tenant
from app.services import evolution_client, handover, invalid_contact
from app.services.attribution import _phone_variants

BATCH = 40


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def main() -> None:
    key = _arg("--cliente")
    apply_changes = "--aplicar" in sys.argv
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        number = next((n for n in tenant.whatsapp_numbers if handover.usable(n) and n.provider == "evolution"), None)
        if number is None:
            print("⚠️ Nenhum WhatsApp conectado neste cliente pra fazer a consulta.")
            return
        ids = {lid for (lid,) in db.query(Conversation.lead_id).join(Message, Message.conversation_id == Conversation.id)
               .filter(Conversation.tenant_id == tenant.id, Message.direction == "out", Message.body.like("⚠️ Não enviada%")).distinct()}
        leads = [l for l in db.query(Lead).filter(Lead.id.in_(ids), Lead.is_group.is_(False), Lead.deleted_at.is_(None))
                 if invalid_contact.looks_like_phone(l.phone)]
        print(f"\n{tenant.name}: conferindo {len(leads)} número(s) com mensagem que não saiu (pelo {number.label})...\n")
        variants = {l.id: sorted(_phone_variants(re.sub(r"\D", "", l.phone))) for l in leads}
        wanted = sorted({v for vs in variants.values() for v in vs})
        exists: dict = {}
        for i in range(0, len(wanted), BATCH):
            try:
                exists.update(evolution_client.whatsapp_numbers(number.evolution_instance, wanted[i:i + BATCH]))
            except evolution_client.EvolutionError as exc:
                print(f"⚠️ Não consegui consultar agora: {exc}")
                return
            time.sleep(1)
        invalid, fixed, ok = [], [], 0
        for lead in leads:
            found = next((v for v in variants[lead.id] if exists.get(v) or exists.get(v[2:] if v.startswith("55") else v)), None)
            if found is None:
                invalid.append(lead)
            elif found != re.sub(r"\D", "", lead.phone):
                fixed.append((lead, found))
            else:
                ok += 1
        for lead in invalid:
            print(f"📵 {lead.name or '-'} ({lead.phone}): não tem WhatsApp -> Perdido · Contato inválido")
        for lead, found in fixed:
            print(f"🔧 {lead.name or '-'}: {lead.phone} -> {found}")
        print(f"\nResumo: {len(invalid)} sem WhatsApp, {len(fixed)} com o telefone corrigido, {ok} com número certo "
              "(a falha foi o WhatsApp do vendedor desconectado).")
        if apply_changes:
            for lead in invalid:
                invalid_contact.mark(db, lead)
            for lead, found in fixed:
                lead.phone = found
            db.commit()
            print("✅ Aplicado.")
        else:
            print("Nada foi mudado. Pra aplicar, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
