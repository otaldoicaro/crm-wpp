"""Recupera mensagens que o CRM não registrou, a partir do que o Evolution guardou.

Rodar no terminal do VPS (Web console da Hostinger):
    docker exec -it crm-app python -m scripts.recuperar_historico            # últimos 15 dias, só mostra
    docker exec -it crm-app python -m scripts.recuperar_historico 30         # últimos 30 dias
    docker exec -it crm-app python -m scripts.recuperar_historico 30 --aplicar   # importa

Não duplica nada (compara pelo id da mensagem) e mantém a hora original de cada mensagem.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.services import history_sync


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 15
    apply = "--aplicar" in sys.argv
    print(f"\n{'Importando' if apply else 'Conferindo'} mensagens dos últimos {days} dia(s) guardadas no Evolution...\n")
    total = 0
    for label, st in history_sync.sync_all(days, apply).items():
        if "erro" in st:
            print(f"• {label}: ⚠️  {st['erro']}\n")
            continue
        missing = st["importar_cliente"] + st["importar_nossas"]
        total += missing
        print(f"• {label}: {st['vistas']} mensagens no Evolution · {st['ja_no_crm']} já estavam no CRM · "
              f"faltavam {missing} ({st['importar_cliente']} do cliente, {st['importar_nossas']} nossas)"
              + (f" · {st['novos_leads']} lead(s) novo(s)" if st["novos_leads"] else ""))
        for name, n in sorted(st["leads"].items(), key=lambda kv: -kv[1])[:15]:
            print(f"     {name}: {n}")
        if len(st["leads"]) > 15:
            print(f"     … e mais {len(st['leads']) - 15} conversa(s)")
        print()
    if total and not apply:
        print(f"Faltavam {total} mensagem(ns). Pra importar, rode de novo com --aplicar no final.")
    elif total:
        print(f"Pronto: {total} mensagem(ns) recuperada(s).")
    else:
        print("Nada faltando.")


if __name__ == "__main__":
    main()
