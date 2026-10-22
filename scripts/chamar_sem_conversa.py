"""🤖 Chama, uma vez, os leads antigos que preencheram o formulário e nunca falaram no WhatsApp.

Mesma mensagem da automação (⚙ Prazos → 🤖 Mensagem automática): confirma a peça e o ano e diz
que vai verificar. Sai do WhatsApp do vendedor já atribuído; sem vendedor ou com o WhatsApp dele
desconectado, vai pro próximo do rodízio (e o lead passa pra ele). O lead vai pra Qualificado.
Lead que está com admin (ou com quem saiu/pausou) é redistribuído entre os vendedores ANTES de
começar a mandar, sempre pro vendedor com menos leads da lista, e já aparece no Pipeline dele.
Com --equilibrar, quem tem mais que a sua parte (ex: 73 enquanto os outros têm 20) também passa o
excesso pros outros: todo mundo fica com quantidades parecidas e ninguém bate o limite do dia, então
termina tudo bem antes (o cliente ainda não falou com ninguém, trocar o vendedor não muda nada pra ele).

Cuidados (é WhatsApp de celular, não a API oficial: mandar muito de uma vez pode bloquear o número):
- só um cliente por vez (--cliente <identificador>, ex: novaviseu);
- só formulários dos últimos N dias (padrão 30) e que têm a peça;
- uma mensagem a cada 40-90 segundos (sorteado) e no máximo --por-numero por WhatsApp por dia (padrão 40);
- só das 8h às 21h; fora disso espera o dia seguinte sozinho;
- nunca manda duas vezes pro mesmo lead (pode parar e rodar de novo).

Sem --aplicar só mostra quantos são, de quem, e um exemplo de mensagem por vendedor.

    docker exec -it crm-app python -m scripts.chamar_sem_conversa --cliente novaviseu
    docker exec -d crm-app sh -c 'python -m scripts.chamar_sem_conversa --cliente novaviseu --equilibrar --aplicar > /tmp/chamar.log 2>&1'
    docker exec crm-app tail -n 15 /tmp/chamar.log
"""

import datetime
import fcntl
import os
import random
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Lead, Tenant
from app.services import first_contact
from app.timeutil import to_local

PAUSE = (40, 90)  # segundos entre uma mensagem e outra (sorteado: parece gente, não robô)


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def balance(leads: list, sellers: list, even: bool) -> dict:
    """{lead_id: novo vendedor}. Quem não está com vendedor que pode mandar vai pro que tem menos;
    com even=True, quem passou da média também solta o excesso (os mais novos primeiro)."""
    owners = list(dict.fromkeys(n.owner_user_id for n in sellers))
    if not owners:
        return {}
    load = Counter({o: 0 for o in owners})
    free = []
    for lead in leads:
        if lead.assigned_user_id in load and first_contact.is_seller(lead.assigned_user):
            load[lead.assigned_user_id] += 1
        else:
            free.append(lead)
    if even:
        target = -(-len(leads) // len(owners))
        for owner in owners:
            mine = [l for l in leads if l.assigned_user_id == owner and l not in free]
            extra = load[owner] - target
            if extra > 0:
                free += mine[:extra]  # a lista vem do mais novo pro mais antigo
                load[owner] -= extra
    plan = {}
    for lead in free:
        owner = min(owners, key=lambda o: load[o])
        load[owner] += 1
        if owner != lead.assigned_user_id:
            plan[lead.id] = owner
    return plan


def _wait_for_hours() -> None:
    """Fora do horário: dorme até as 8h (de 10 em 10 min, pra aparecer no log)."""
    while not first_contact.HOURS[0] <= to_local(datetime.datetime.utcnow()).hour < first_contact.HOURS[1]:
        print(f"⏸ fora do horário ({first_contact.HOURS[0]}h às {first_contact.HOURS[1]}h); continuo depois", flush=True)
        time.sleep(600)


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    key = _arg("--cliente")
    days = int(_arg("--dias", "30"))
    per_number = int(_arg("--por-numero", "40"))
    apply_changes = "--aplicar" in sys.argv
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        query = first_contact.waiting_query(db, tenant)
        leads = query.filter(Lead.created_at >= since).order_by(Lead.created_at.desc()).all() if query is not None else []
        older = query.filter(Lead.created_at < since).count() if query is not None else 0
        print(f"\n{tenant.name}: {len(leads)} lead(s) sem conversa, com a peça, dos últimos {days} dias"
              + (f" ({older} mais antigos ficam de fora)" if older else "") + ".\n")
        by_seller = Counter((l.assigned_user.name if l.assigned_user else "sem vendedor") for l in leads
                            if first_contact.is_seller(l.assigned_user))
        for name, n in by_seller.most_common():
            print(f"  {n:4d} · {name}")
        moving = [l for l in leads if not first_contact.is_seller(l.assigned_user)]
        sellers = first_contact.seller_numbers(db, tenant.id)
        if moving:
            who = Counter((l.assigned_user.name + (" (admin)" if l.assigned_user.role == "admin" else " (fora do rodízio)"))
                          if l.assigned_user else "sem vendedor" for l in moving)
            print(f"\n  {len(moving)} estão com admin / sem vendedor / com quem saiu e vão ser redistribuídos entre "
                  f"{len(sellers)} vendedor(es) com WhatsApp conectado ({', '.join(n.owner.name for n in sellers) or 'nenhum'}):")
            for name, n in who.most_common():
                print(f"       {n:4d} · {name}")
        if not leads:
            return
        plan = balance(leads, sellers, "--equilibrar" in sys.argv)
        if plan:
            after = Counter(l.assigned_user_id for l in leads)
            for lead_id, owner in plan.items():
                after[next(l.assigned_user_id for l in leads if l.id == lead_id)] -= 1
                after[owner] += 1
            names = {n.owner_user_id: n.owner.name for n in sellers}
            print(f"\n  Depois de redistribuir ({len(plan)} trocam de vendedor):")
            for owner, n in sorted(((o, n) for o, n in after.items() if o in names), key=lambda x: -x[1]):
                print(f"  {n:4d} · {names[owner]}")
            busiest = max((n for o, n in after.items() if o in names), default=0)
            if busiest > per_number and "--equilibrar" not in sys.argv:
                print(f"  (alguém fica com mais de {per_number}: o excesso sai no dia seguinte. Com --equilibrar, divide tudo por igual.)")
        days_needed = -(-len(leads) // max(1, per_number * max(1, len(sellers))))
        print(f"\nRitmo: 1 mensagem a cada {PAUSE[0]}-{PAUSE[1]}s, até {per_number} por WhatsApp por dia "
              f"(≈ {days_needed} dia(s) pra todos, se os WhatsApps estiverem conectados).")
        sample = leads[0]
        seller = sample.assigned_user if first_contact.is_seller(sample.assigned_user) else (sellers[0].owner if sellers else None)
        print("\nExemplo de mensagem (" + (sample.name or sample.phone) + "):\n")
        print("  " + first_contact.message_for(tenant, sample, seller).replace("\n", "\n  "))
        if not apply_changes:
            print("\nNada foi enviado nem redistribuído. Pra mandar, rode de novo com --aplicar no final.")
            return
        # trava: dois envios ao mesmo tempo poderiam mandar a mesma mensagem 2x pro mesmo cliente
        lock = open("/tmp/chamar_sem_conversa.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("\n⚠️ Já tem um envio rodando. Esse aqui não vai mandar nada (pra não repetir mensagem).")
            return
        if plan:  # redistribui antes de mandar
            from app.services import ownership

            for lead in leads:
                if lead.id in plan:
                    previous = lead.assigned_user_id
                    lead.assigned_user_id = plan[lead.id]
                    ownership.changed(db, lead, previous, "redistribuição pra chamar os leads sem conversa")
            db.commit()
            print(f"\n↪ {len(plan)} lead(s) redistribuídos entre os vendedores.")

        sent_today: Counter = Counter()
        day = to_local(datetime.datetime.utcnow()).date()
        results = Counter()
        pending = [l.id for l in reversed(leads)]  # do mais antigo pro mais novo
        total, done = len(pending), 0
        while pending:
            later = []  # WhatsApp que bateu o limite do dia: fica pro dia seguinte
            for lead_id in pending:
                _wait_for_hours()
                today = to_local(datetime.datetime.utcnow()).date()
                if today != day:
                    sent_today.clear()
                    day = today
                db.expire_all()
                lead = db.get(Lead, lead_id)
                if lead is None or lead.auto_hello_at or lead.conversations:
                    results["já tinha conversa"] += 1  # a pessoa chamou nesse meio tempo, ou já recebeu
                    continue
                number = first_contact.number_for(db, lead)
                if number is None:
                    db.commit()
                    results["sem WhatsApp conectado"] += 1
                    print(f"⚠️ {lead.name or lead.phone}: nenhum WhatsApp conectado pra mandar", flush=True)
                    continue
                if sent_today[number.id] >= per_number:
                    db.rollback()
                    later.append(lead_id)
                    continue
                result = first_contact.send_one(db, tenant, lead, number)
                results[result] += 1
                done += 1
                if result in ("enviada", "confirmando"):
                    sent_today[number.id] += 1
                print(f"{done}/{total} · {lead.name or lead.phone} · {number.label} · {result}", flush=True)
                time.sleep(random.uniform(*PAUSE))
            pending = later
            if pending:
                print(f"⏸ {len(pending)} ficaram pro limite do dia ({per_number} por WhatsApp); continuo amanhã às "
                      f"{first_contact.HOURS[0]}h", flush=True)
                while to_local(datetime.datetime.utcnow()).date() == day:
                    time.sleep(600)
        print("\nResumo:", ", ".join(f"{n} {k}" for k, n in results.most_common()))
    finally:
        db.close()


if __name__ == "__main__":
    main()
