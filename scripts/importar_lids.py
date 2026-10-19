"""Importa a tabela LID -> telefone que o próprio WhatsApp conectado guarda dentro do Evolution:
no Redis do Evolution (lido direto, com EVOLUTION_REDIS_URL) ou, sem Redis, nos arquivos
lid-mapping-<lid>_reverse.json de cada número (recebidos pela entrada padrão). É a única fonte confiável: o
Evolution não guarda essa ligação no banco dele, e a mensagem que o vendedor manda pelo
celular numa conversa por LID chega sem o telefone.

Não roda sozinho: recebe os arquivos pela entrada padrão. O instalador e o agendamento
(/etc/cron.d/crm-lids, a cada 15 min) fazem assim, no terminal do VPS:
    docker exec <evolution> sh -c 'find /evolution/instances -name "lid-mapping-*_reverse.json" -exec grep -H "" {} +' \
      | docker exec -i crm-app python -m scripts.importar_lids
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import LidMap

LINE = re.compile(r"lid-mapping-(\d+)_reverse\.json:\s*\"?(\d{8,15})")


def main() -> None:
    quiet = "--quieto" in sys.argv
    found = {}
    from app.services import lid_store

    found.update(lid_store.all_mappings())  # Redis do Evolution (onde ficam quando o Redis está ligado)
    for line in ([] if sys.stdin.isatty() else sys.stdin):  # arquivos (quando o Evolution não usa Redis)
        m = LINE.search(line)
        if m:
            found[m.group(1)] = m.group(2)
    db = SessionLocal()
    try:
        known = {row.lid: row for row in db.query(LidMap)}
        new = changed = 0
        for lid, phone in found.items():
            row = known.get(lid)
            if row is None:
                db.add(LidMap(lid=lid, phone=phone))
                new += 1
            elif row.phone != phone:
                row.phone = phone
                changed += 1
        db.commit()
    finally:
        db.close()
    if not quiet or new or changed:
        print(f"Códigos do WhatsApp (LID) lidos: {len(found)} · novos no CRM: {new} · corrigidos: {changed}")
    # leads que nasceram com o código no lugar do telefone: junta no lead certo
    from app.services.lead_match import merge_lid_leads

    db = SessionLocal()
    try:
        merged = merge_lid_leads(db)
    finally:
        db.close()
    for name, action, before, after in merged:
        print(f"  {'🔗 juntou no cadastro certo' if action == 'juntou' else '📞 telefone corrigido'}: {name}"
              + (f" (estava com {before}, ficou com {after})" if action == "juntou" and before != after else ""))
    if merged or not quiet:
        print(f"Cadastros com código no lugar do telefone resolvidos: {len(merged)}")
    if not found and not quiet:
        print("Nenhum código LID encontrado (nem no Redis nem nos arquivos do Evolution). Mande este resultado pro Claude.")


if __name__ == "__main__":
    main()
