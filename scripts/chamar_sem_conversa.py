"""🤖 Chama, uma vez, os leads antigos que preencheram o formulário e nunca falaram no WhatsApp.

Mesma mensagem da automação (⚙ Prazos → 🤖 Mensagem automática): confirma a peça e o ano e diz
que vai verificar. Sai do WhatsApp do vendedor já atribuído; sem vendedor ou com o WhatsApp dele
desconectado, vai pro próximo do rodízio (e o lead passa pra ele). O lead vai pra Qualificado.

Cuidados (é WhatsApp de celular, não a API oficial: mandar muito de uma vez pode bloquear o número):
- só um cliente por vez (--cliente <identificador>, ex: novaviseu);
- só formulários dos últimos N dias (padrão 30) e que têm a peça;
- uma mensagem a cada 40-90 segundos (sorteado) e no máximo --por-numero por WhatsApp por dia (padrão 40);
- só das 8h às 21h; fora disso espera o dia seguinte sozinho;
- nunca manda duas vezes pro mesmo lead (pode parar e rodar de novo).

Sem --aplicar só mostra quantos são, de quem, e um exemplo de mensagem por vendedor.

    docker exec -it crm-app python -m scripts.chamar_sem_conversa --cliente novaviseu
    docker exec -d crm-app sh -c 'python -m scripts.chamar_sem_conversa --cliente novaviseu --aplicar > /tmp/chamar.log 2>&1'
    docker exec crm-app tail -n 15 /tmp/chamar.log
"""

import datetime
import os
import random
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Lead, Tenant, User
from app.services import first_contact
from app.timeutil import to_local

PAUSE = (40, 90)  # segundos entre uma mensagem e outra (sorteado: parece gente, não robô)


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


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
        by_seller = Counter((l.assigned_user.name if l.assigned_user else "sem vendedor (vai pro rodízio)") for l in leads)
        for name, n in by_seller.most_common():
            print(f"  {n:4d} · {name}")
        if not leads:
            return
        days_needed = -(-len(leads) // max(1, per_number * max(1, len(by_seller))))
        print(f"\nRitmo: 1 mensagem a cada {PAUSE[0]}-{PAUSE[1]}s, até {per_number} por WhatsApp por dia "
              f"(≈ {days_needed} dia(s) pra todos, se os WhatsApps estiverem conectados).")
        sample = leads[0]
        seller = sample.assigned_user or db.query(User).filter(User.tenant_id == tenant.id).first()
        print("\nExemplo de mensagem (" + (sample.name or sample.phone) + "):\n")
        print("  " + first_contact.message_for(tenant, sample, seller).replace("\n", "\n  "))
        if not apply_changes:
            print("\nNada foi enviado. Pra mandar, rode de novo com --aplicar no final.")
            return

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
