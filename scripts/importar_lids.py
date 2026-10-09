"""Importa a tabela LID -> telefone que o próprio WhatsApp conectado guarda dentro do Evolution
(arquivos lid-mapping-<lid>_reverse.json de cada número). É a única fonte confiável: o
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
    for line in sys.stdin:
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
    if not found and not quiet:
        print("Nenhum arquivo de LID encontrado no Evolution. Mande este resultado pro Claude.")


if __name__ == "__main__":
    main()
