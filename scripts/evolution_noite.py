"""Renovação do Evolution de madrugada (o instalador agenda pras 4h de Brasília; ninguém precisa rodar).

    docker exec crm-app python -m scripts.evolution_noite antes    # guarda quem funciona e pausa o vigia
    (o agendamento reinicia o Evolution)
    docker exec crm-app python -m scripts.evolution_noite depois   # confere cada número e religa/avisa

Número que não voltar fica pronto pro QR code e o vendedor e os admins recebem o aviso no CRM.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import WhatsAppNumber
from app.services import evolution_client, wa_health


def _active(db) -> list:
    return db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == "evolution",
                                           WhatsAppNumber.is_active.is_(True)).all()


def before() -> None:
    with SessionLocal() as db:
        working = []
        for number in _active(db):
            try:
                if evolution_client.connection_state(number.evolution_instance) == "open":
                    working.append(number.id)
            except evolution_client.EvolutionError:
                pass
    with open(wa_health.NIGHT_FILE, "w") as f:
        json.dump(working, f)
    print(f"{len(working)} número(s) conectado(s) antes de reiniciar o Evolution")


def after() -> None:
    try:
        with open(wa_health.NIGHT_FILE) as f:
            ids = json.load(f)
    except (OSError, ValueError):
        ids = []
    try:
        with SessionLocal() as db:
            for number_id in ids:
                number = db.get(WhatsAppNumber, number_id)
                if number is None or not number.is_active:
                    continue
                try:
                    print(f"• {number.label}: {wa_health.after_night_restart(db, number)}")
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    print(f"• {number.label}: erro {exc}")
    finally:
        try:
            os.remove(wa_health.NIGHT_FILE)  # o vigia volta a trabalhar
        except OSError:
            pass


if __name__ == "__main__":
    {"antes": before, "depois": after}[sys.argv[1] if len(sys.argv) > 1 else "depois"]()
