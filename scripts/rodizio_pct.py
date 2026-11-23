"""🎯 Grava a % do rodízio de vários vendedores de uma vez (o mesmo campo "%" da tela Equipe).

No terminal do VPS (1º argumento = identificador do cliente; depois Nome=% pelo primeiro nome):
    docker exec -i crm-app python -m scripts.rodizio_pct novaviseu "André=20" "Davi=5" "Dener=10"

Confere o nome sem acento e sem maiúscula; nome que bate com 2 pessoas ou com ninguém não grava nada.
No fim mostra quanto cada um que está no rodízio recebe e avisa quem ficou sem % (recebe a parte igual).
"""

import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Tenant, User
from app.services.distribution import weights


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower()) if unicodedata.category(c) != "Mn").strip()


def main() -> None:
    if len(sys.argv) < 3:
        print('use: python -m scripts.rodizio_pct <identificador do cliente> "Nome=20" "Outro=5" ...')
        return
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.subdomain == sys.argv[1]).first()
        if tenant is None:
            print(f"cliente '{sys.argv[1]}' não encontrado")
            return
        people = [u for u in db.query(User).filter(User.tenant_id == tenant.id, User.removed_at.is_(None),
                                                    User.pending_approval.is_(False))]
        changes, problems = [], []
        for arg in sys.argv[2:]:
            name, _, pct = arg.partition("=")
            if not pct.strip().isdigit():
                problems.append(f"'{arg}': use Nome=número")
                continue
            key = _plain(name)
            found = [u for u in people if _plain(u.name) == key] or [u for u in people if _plain(u.name).split(" ")[0] == key]
            if len(found) != 1:
                problems.append(f"'{name}': {'ninguém com esse nome' if not found else 'mais de um: ' + ', '.join(u.name for u in found)}")
                continue
            changes.append((found[0], min(100, int(pct))))
        if problems:
            print("Nada foi gravado. Arrume e rode de novo:")
            for p in problems:
                print("  ⚠️", p)
            return
        for user, pct in changes:
            user.rodizio_peso, user.rodizio_half, user.rodizio_skip = pct, False, False
        db.commit()
        total = sum(p for _, p in changes)
        print(f"Gravado: {len(changes)} vendedores, soma {total}%{'' if total == 100 else ' (não precisa dar 100: vale a proporção)'}")
        turn = [u for u in people if u.is_active and u.accepting_leads and not u.on_leave and not u.read_only]
        w = weights(turn)
        all_w = sum(w.values()) or 1
        print("\nNo rodízio agora (quem está desconectado não recebe; a parte dele vai pros outros):")
        for u in sorted(turn, key=lambda u: -w[u.id]):
            print(f"  {u.name:30} {u.rodizio_peso or 'igual':>5}{'%' if u.rodizio_peso else ''}  → ≈ {100 * w[u.id] / all_w:.0f}%")
        without = [u.name for u in turn if not u.rodizio_peso]
        if without:
            print("\n⚠️ Sem % (recebem a parte igual):", ", ".join(without),
                  "\n   Pra tirar do rodízio: Equipe → botão 'No rodízio' (vira Pausado).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
