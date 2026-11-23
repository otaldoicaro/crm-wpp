"""🔧 Liga ou desliga um recurso (o mesmo de ⚙ Configurações → Recursos) em um cliente ou em todos.

No terminal do VPS:
    docker exec -i crm-app python -m scripts.recurso uazapi todos ligar
    docker exec -i crm-app python -m scripts.recurso uazapi novaviseu desligar

Quem nunca mexeu nos recursos continua com os do ramo dele; este script só acrescenta (ou tira) o pedido.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app import features
from app.db import SessionLocal
from app.models import Tenant


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[3] not in ("ligar", "desligar"):
        print("use: python -m scripts.recurso <recurso> <identificador do cliente | todos> <ligar | desligar>")
        return
    key, who, action = sys.argv[1:]
    if key not in features.FEATURES:
        print(f"recurso '{key}' não existe. Os que existem: {', '.join(features.FEATURES)}")
        return
    db = SessionLocal()
    try:
        query = db.query(Tenant)
        if who != "todos":
            query = query.filter(Tenant.subdomain == who)
        tenants = query.order_by(Tenant.name).all()
        if not tenants:
            print(f"cliente '{who}' não encontrado")
            return
        for tenant in tenants:
            keys = features.enabled(tenant)
            before = key in keys
            keys = keys | {key} if action == "ligar" else keys - {key}
            tenant.features = features.store(keys)
            db.add(tenant)
            print(f"  {'✅' if action == 'ligar' else '⛔'} {tenant.name}{' (já estava)' if before == (action == 'ligar') else ''}")
        db.commit()
        print(f"\nPronto: '{key}' {'ligado' if action == 'ligar' else 'desligado'} em {len(tenants)} cliente(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
