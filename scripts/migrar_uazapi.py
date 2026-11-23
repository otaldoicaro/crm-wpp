"""📱 Passa os WhatsApps do Evolution pra UAZapi de uma vez (um cliente ou todos), sem buraco no atendimento.

No terminal do VPS (sem --aplicar só mostra o que vai fazer):
    docker exec -i crm-app python -m scripts.migrar_uazapi todos
    docker exec -i crm-app python -m scripts.migrar_uazapi todos --aplicar
    docker exec -i crm-app python -m scripts.migrar_uazapi novaviseu --aplicar
Desfazer (só os que ainda não leram o QR da UAZapi):
    docker exec -i crm-app python -m scripts.migrar_uazapi todos --voltar

Como funciona, número por número:
1. cria a instância na UAZapi (com o webhook do CRM) e marca o número como "passando" (migrating);
2. o Evolution CONTINUA recebendo e enviando por ele até o vendedor ler o QR novo;
3. o vendedor recebe o aviso no CRM: Meu WhatsApp → Conectar agora → ler o QR code;
4. leu: o CRM puxa as últimas 24h pela UAZapi e tira o Evolution do celular sozinho.
Também liga o recurso "📱 WhatsApp pela UAZAPI" nos clientes.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app import features
from app.db import SessionLocal
from app.models import Tenant, WhatsAppNumber
from app.services import evolution_client, uazapi_client, wa_health

WARN_TITLE = "📱 Leia o QR code do seu WhatsApp de novo"
WARN_TEXT = ("O WhatsApp do CRM mudou pra um sistema mais estável. Entre em Meu WhatsApp → Conectar agora e leia o "
             "QR code no celular (WhatsApp → ⋮ → Aparelhos conectados → Conectar um aparelho). Até ler, continua "
             "tudo funcionando como antes.")


def _owner(db, number) -> str:
    return number.owner.name if number.owner is not None else "sem dono (admins)"


def migrate(db, number) -> str:
    token = uazapi_client.create_instance(number.evolution_instance)
    try:
        uazapi_client.set_webhook(token)
    except uazapi_client.UazapiError:
        try:
            uazapi_client.delete_instance(token)
        except uazapi_client.UazapiError:
            pass
        raise
    number.provider, number.access_token, number.connection_state, number.migrating = (
        uazapi_client.PROVIDER, token, "close", True)
    db.add(number)
    wa_health._warn(db, number, wa_health._admins_and_owner(db, number), WARN_TITLE, WARN_TEXT)
    db.commit()
    return "ok"


def back(db, number) -> None:
    for action in (uazapi_client.disconnect, uazapi_client.delete_instance):
        try:
            action(number.access_token)
        except uazapi_client.UazapiError:
            pass
    number.provider, number.access_token, number.migrating = "evolution", "", False
    try:  # o Evolution nunca saiu: volta com o estado que ele tem agora
        number.connection_state = evolution_client.connection_state(number.evolution_instance)
    except evolution_client.EvolutionError:
        number.connection_state = "close"
    db.add(number)
    db.commit()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply, undo = "--aplicar" in sys.argv, "--voltar" in sys.argv
    if len(args) != 1:
        print("use: python -m scripts.migrar_uazapi <identificador do cliente | todos> [--aplicar | --voltar]")
        return
    if not uazapi_client.is_configured():
        print("A UAZapi não está configurada neste servidor. Rode antes o configurar_uazapi.sh.")
        return
    db = SessionLocal()
    try:
        query = db.query(Tenant)
        if args[0] != "todos":
            query = query.filter(Tenant.subdomain == args[0])
        tenants = query.order_by(Tenant.name).all()
        if not tenants:
            print(f"cliente '{args[0]}' não encontrado")
            return
        done = failed = 0
        for tenant in tenants:
            if undo:
                rows = db.query(WhatsAppNumber).filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.migrating.is_(True)).all()
            else:
                rows = (db.query(WhatsAppNumber).filter(WhatsAppNumber.tenant_id == tenant.id, WhatsAppNumber.provider == "evolution",
                                                        WhatsAppNumber.is_active.is_(True)).order_by(WhatsAppNumber.label).all())
            if not rows:
                continue
            print(f"\n{tenant.name}:")
            if apply and not undo and "uazapi" not in features.enabled(tenant):
                tenant.features = features.store(features.enabled(tenant) | {"uazapi"})
                db.add(tenant)
                db.commit()
            for number in rows:
                line = f"  {number.label:28} {_owner(db, number):24}"
                if undo:
                    back(db, number)
                    print(line, "↩️ voltou pro Evolution")
                    done += 1
                elif not apply:
                    print(line, "→ vai passar" + ("" if number.connection_state == "open" else " (está desconectado no Evolution)"))
                else:
                    try:
                        migrate(db, number)
                        print(line, "✅ pronto: o vendedor precisa ler o QR")
                        done += 1
                    except uazapi_client.UazapiError as exc:
                        db.rollback()
                        print(line, f"⚠️ não passou ({exc}); continua no Evolution")
                        failed += 1
        if undo:
            print(f"\n{done} número(s) voltaram pro Evolution.")
        elif not apply:
            print("\nNada foi mudado. Pra passar de verdade, rode de novo com --aplicar.")
        else:
            print(f"\n{done} número(s) passando pra UAZapi{f', {failed} com problema (continuam no Evolution)' if failed else ''}.")
            print("Cada vendedor recebeu o aviso no CRM: Meu WhatsApp → Conectar agora → ler o QR code.")
            print("Até ler, o número continua funcionando pelo Evolution.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
