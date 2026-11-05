"""🔍 Varredura (só leitura): que frases aparecem nas vendas que fecharam e quase nunca nas que não fecharam.

Compara, de um cliente, nos últimos N dias (padrão 90):
- GANHOS: leads que chegaram em Ganho — mensagens dos 3 dias antes de virar Ganho;
- NÃO FECHARAM: leads que receberam preço ("R$") e não viraram Ganho (perdidos, parados, sem resposta).

Mostra:
1. frases do CLIENTE e do TIME que mais aparecem nos ganhos e pouco nos que não fecharam (candidatas a gatilho);
2. como as frases de "💡 parece que a venda fechou" de hoje se saem (quantas vezes acertaram);
3. quantos ganhos o cliente não respondeu nada depois do preço (venda marcada sem conversa).
Não muda nada no CRM. Pode rodar quantas vezes quiser (de preferência fora do horário comercial).

    docker exec -it crm-app python -m scripts.frases_venda --cliente novaviseu
    docker exec -it crm-app python -m scripts.frases_venda --cliente novaviseu --dias 60
"""

import datetime
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, PipelineStage, Tenant
from app.services import suggestions

BEFORE_WON = datetime.timedelta(days=3)
STOP = set("a o as os e de da do das dos em no na nos nas um uma pra pro para por com que se eu voce vc ja ai la ta "
           "me te ele ela isso esse essa este esta tem ter foi ser e ok sim nao mais bom dia boa tarde noite obrigado "
           "obrigada blz beleza tudo bem oi ola".split())


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def grams(text: str) -> set:
    """Pedaços de 2 a 4 palavras da mensagem (normalizada: sem acento, minúscula), sem os só de palavra vazia."""
    words = re.findall(r"[a-z]+", suggestions._norm(text))
    out = set()
    for n in (2, 3, 4):
        for i in range(len(words) - n + 1):
            chunk = words[i:i + n]
            if sum(1 for w in chunk if w not in STOP and len(w) > 2) >= 1:
                out.add(" ".join(chunk))
    return out


def ranking(won: dict, lost: dict, n_won: int, n_lost: int, top: int) -> list:
    """[(frase, % nos ganhos, % nos que não fecharam, ganhos)] das que mais separam ganho de não ganho."""
    rows = []
    for phrase, count in won.items():
        if count < max(3, n_won // 40):
            continue
        p_won, p_lost = count / n_won, lost.get(phrase, 0) / max(1, n_lost)
        if p_won < 2 * p_lost:
            continue  # aparece quase igual nos dois: não diz nada
        rows.append((phrase, p_won, p_lost, count, p_won / (p_lost + 0.02)))
    rows.sort(key=lambda r: -r[4])
    picked = []
    for row in rows:  # tira repetição ("vou buscar" e "vou buscar amanha": fica a mais forte)
        pairs = {" ".join(row[0].split()[i:i + 2]) for i in range(len(row[0].split()) - 1)}
        if not any(any(pair in p[0] for pair in pairs) for p in picked):
            picked.append(row)
        if len(picked) >= top:
            break
    return picked


def main() -> None:
    key, days = _arg("--cliente"), int(_arg("--dias", "90"))
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        won_ids = {s.id for s in db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id, PipelineStage.is_won.is_(True))}
        leads = db.query(Lead).filter(Lead.tenant_id == tenant.id, Lead.created_at >= since, Lead.is_group.is_(False),
                                      Lead.deleted_at.is_(None), Lead.tag.notin_(("outro", "time"))).all()
        lead_by_id = {l.id: l for l in leads}
        msgs = defaultdict(list)
        rows = (db.query(Conversation.lead_id, Message.direction, Message.body, Message.created_at)
                .join(Message, Message.conversation_id == Conversation.id)
                .filter(Conversation.tenant_id == tenant.id, Conversation.lead_id.in_(list(lead_by_id)),
                        Message.direction.in_(("in", "out")), Message.body != "")
                .yield_per(2000))
        for lead_id, direction, body, when in rows:
            msgs[lead_id].append((when, direction, body))

        won_in, won_out, lost_in, lost_out = Counter(), Counter(), Counter(), Counter()
        n_won = n_lost = silent_won = 0
        rule_hits = defaultdict(lambda: [0, 0])  # frase de ganho -> [leads que fecharam, que não fecharam]
        segment = tenant.segment or "autopecas"
        for lead_id, items in msgs.items():
            lead = lead_by_id[lead_id]
            items.sort(key=lambda m: m[0])
            is_won = lead.stage_id in won_ids
            if is_won:
                end = (lead.stage_entered_at or items[-1][0]) + datetime.timedelta(hours=1)
                window = [m for m in items if end - BEFORE_WON - datetime.timedelta(hours=1) <= m[0] <= end]
                n_won += 1
                last_price = max((m[0] for m in items if m[1] == "out" and "r$" in m[2].lower() and m[0] <= end), default=None)
                if last_price and not any(m[1] == "in" and m[0] > last_price and m[0] <= end for m in items):
                    silent_won += 1
            else:
                if not any(m[1] == "out" and "r$" in m[2].lower() for m in items):
                    continue  # nem recebeu preço: não serve de comparação
                window = items
                n_lost += 1
            g_in, g_out = set(), set()
            hit_rules = set()
            for _, direction, body in window:
                (g_in if direction == "in" else g_out).update(grams(body))
                hit = suggestions.detect(body, direction == "out", segment)
                if hit and hit[0] == "ganho":
                    hit_rules.add(f"{'cliente' if direction == 'in' else 'time'}: “{hit[1]}”")
            (won_in if is_won else lost_in).update(g_in)
            (won_out if is_won else lost_out).update(g_out)
            for rule in hit_rules:
                rule_hits[rule][0 if is_won else 1] += 1

        print(f"\n{tenant.name} · últimos {days} dias · {n_won} ganhos e {n_lost} que receberam preço e não fecharam\n")
        if n_won < 5:
            print("Poucos ganhos pra comparar. Tente com --dias 180.")
            return
        print("== 1. Frases do CLIENTE que mais aparecem nas vendas fechadas (3 dias antes do Ganho) ==")
        print("   frase · % dos ganhos · % dos que não fecharam")
        for phrase, p_won, p_lost, _, _ in ranking(won_in, lost_in, n_won, n_lost, 40):
            print(f"   “{phrase}” · {p_won:.0%} · {p_lost:.0%}")
        print("\n== 2. Frases do TIME que mais aparecem nas vendas fechadas ==")
        for phrase, p_won, p_lost, _, _ in ranking(won_out, lost_out, n_won, n_lost, 25):
            print(f"   “{phrase}” · {p_won:.0%} · {p_lost:.0%}")
        print("\n== 3. Como se saem as frases de hoje do “💡 parece que a venda fechou” ==")
        print("   frase · vezes que fechou · vezes que NÃO fechou · acerto")
        for rule, (ok, bad) in sorted(rule_hits.items(), key=lambda r: -(r[1][0] + r[1][1])):
            print(f"   {rule} · {ok} · {bad} · {ok / (ok + bad):.0%}")
        print(f"\n== 4. Ganhos em que o cliente não escreveu nada depois do último preço: {silent_won} de {n_won} ==")
        print("   (venda fechada fora do WhatsApp — loja, telefone — ou marcada como Ganho sem o cliente confirmar)")
        print("\nNada foi mudado no CRM. Mande este resultado pro Claude.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
