"""🔒 Marca contatos como "não espelhar" (conversa pessoal) num cliente: as mensagens com eles deixam de ser
guardadas e a conversa some do Inbox, da busca e da Auditoria pra todos. O que já estava guardado fica
escondido (desmarcando na ficha do lead, volta a aparecer). Telefone que ainda não é lead vira um contato
"Outro" já marcado, pra a conversa nunca entrar.

Sem --aplicar só mostra o que faria.

    docker exec -it crm-app python -m scripts.nao_espelhar --cliente <identificador> --telefones "31 9307-0003, 31 9733-5242"
    docker exec -it crm-app python -m scripts.nao_espelhar --cliente <identificador> --telefones "..." --aplicar
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Lead, Tenant
from app.services.lead_match import _phone_variants, normalize_br_phone


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def main() -> None:
    key, raw = _arg("--cliente"), _arg("--telefones")
    apply_changes = "--aplicar" in sys.argv
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        phones = [re.sub(r"\D", "", p) for p in re.split(r"[,;\n]+", raw) if re.sub(r"\D", "", p)]
        if not phones:
            print('Diga os telefones: --telefones "31 9307-0003, 31 9733-5242"')
            return
        print(f"\n{tenant.name}: {len(phones)} telefone(s)\n")
        for digits in phones:
            phone = normalize_br_phone(digits) or digits
            leads = db.query(Lead).filter(Lead.tenant_id == tenant.id,
                                          Lead.phone.in_(_phone_variants(phone) | _phone_variants(digits))).all()
            if leads:
                names = ", ".join(sorted({l.name or l.phone for l in leads}))
                done = all(l.no_mirror for l in leads)
                print(f"  {digits}: {'já estava marcado' if done else 'vai ser marcado'} · {names}")
                for lead in leads:
                    lead.no_mirror = True
            else:
                print(f"  {digits}: ainda não é contato no CRM · vira contato “Outro” já marcado")
                db.add(Lead(tenant_id=tenant.id, phone=phone, name="", source="manual", tag="outro", no_mirror=True))
        if apply_changes:
            db.commit()
            print("\n✅ Pronto: esses contatos não são mais espelhados no CRM.")
        else:
            db.rollback()
            print("\nNada foi mudado. Pra aplicar, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
