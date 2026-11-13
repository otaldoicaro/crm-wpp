"""↪ Leads passados pra outro vendedor ANTES da aba "Repassados": lê o histórico de trocas de cada lead
("Fulano → Beltrano · transferido por ...") e marca quem passou, pra o lead sair do Inbox dele e ir pra aba
↪ Repassados. Não muda o vendedor de ninguém. Sem --aplicar só mostra quantos são.

    docker exec -it crm-app python -m scripts.repassados_antigos
    docker exec -it crm-app python -m scripts.repassados_antigos --aplicar
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Lead, Tenant, User


def main() -> None:
    apply_changes = "--aplicar" in sys.argv
    db = SessionLocal()
    try:
        for tenant in db.query(Tenant).all():
            by_name = {u.name: u.id for u in db.query(User).filter(User.tenant_id == tenant.id)}
            marked = Counter()
            leads = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.owner_trail.like("%transferido por%"),
                                          Lead.assigned_user_id.isnot(None)).all()
            for lead in leads:
                passed = [i for i in (lead.passed_by or "").split(",") if i]
                for line in (lead.owner_trail or "").splitlines():
                    if "transferido por" not in line or " → " not in line:
                        continue
                    # "P|27/10 14:03 · Fulano → Beltrano · transferido por Admin"
                    parts = line[2:].split(" · ")
                    if len(parts) < 3:
                        continue
                    who = parts[1].split(" → ")[0].strip()
                    user_id = by_name.get(who)
                    if user_id and user_id != lead.assigned_user_id and user_id not in passed:
                        passed.append(user_id)
                        marked[who] += 1
                lead.passed_by = ",".join(passed)[-400:]
            if marked:
                print(f"\n{tenant.name}:")
                for name, n in marked.most_common():
                    print(f"  {n:4d} lead(s) que {name} passou pra outro vão pra aba ↪ Repassados dele")
        if apply_changes:
            db.commit()
            print("\n✅ Pronto.")
        else:
            db.rollback()
            print("\nNada foi mudado. Pra aplicar, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
