"""🔁 Refaz os Ganhos que o CRM marcou sozinho, com a régua nova (mais criteriosa).

Antes, frases como "nota fiscal" + um preço enviado viravam Ganho. Agora Ganho automático só com
pagamento de fato: comprovante conferido, ou frase de pagamento ("pix recebido", "já paguei",
"recebi a peça"...) — e nada depois dizendo que não rolou ("não tenho", "desculpe", "vou ficar
devendo", "ainda tem interesse?").

Pra cada lead em Ganho cuja venda foi marcada pelo CRM (nota "🤖 Revisão automática das etapas:
… → Ganho" ou "🤖 Etapa automática: Ganho"), e que ninguém mexeu depois:
- com prova de pagamento: continua Ganho;
- sem prova: apaga essas notas, tira o valor da venda e volta pra etapa de antes (a da nota, ou
  Negociando); depois as mensagens são relidas com as regras novas (ex: "não tenho" -> Perdido).
  Quem ficou parado 7+ dias vai pra Perdido sozinho na próxima passada do CRM.
Venda confirmada por comprovante (conferido) e Ganho marcado pelo vendedor não são tocados.

Rodar no terminal do VPS (sem --aplicar só mostra o que faria):
    docker exec -it crm-app python -m scripts.refazer_ganhos
    docker exec -d crm-app sh -c 'python -m scripts.refazer_ganhos --aplicar > /tmp/ganhos.log 2>&1'
    docker exec crm-app tail -n 30 /tmp/ganhos.log
"""

import datetime
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, PipelineStage
from app.services import autostage, funnel

SAME_TIME = datetime.timedelta(minutes=10)  # nota e fechamento juntos = foi o CRM que fechou


def _brl(v) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if v else ""


def auto_won_notes(db, lead: Lead, won_name: str) -> list:
    """Notas do CRM fechando este lead como Ganho (revisão em massa ou automática na hora)."""
    notes = (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id == lead.id, Message.direction == "note")
        .order_by(Message.created_at)
        .all()
    )
    return [n for n in notes if (n.body or "").startswith(f"🤖 Etapa automática: {won_name}")
            or ((n.body or "").startswith("🤖 Revisão automática das etapas:") and f"→ {won_name}" in (n.body or ""))]


def paid_proof(db, lead: Lead) -> str:
    """Prova de pagamento na conversa, sem nada depois dizendo que não vendeu. "" = sem prova."""
    msgs = (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id == lead.id, Message.direction.in_(["in", "out"]))
        .order_by(Message.created_at)
        .all()
    )
    if lead.contact_id and lead.created_at:  # negócio novo: só as mensagens dele
        msgs = [m for m in msgs if m.created_at >= lead.created_at - datetime.timedelta(minutes=1)]
    for i, m in enumerate(msgs):
        phrase = autostage.paid_phrase(m.body or "", m.direction == "out")
        if phrase is None:
            continue
        later = autostage.not_sold_after([(x.body or "", x.direction == "out") for x in msgs[i + 1:]])
        if later is None:
            return f"“{phrase}”"
    return ""


def previous_stage(stages: list, note: Message, lead: Lead):
    found = re.search(r"Revisão automática das etapas: (.+?) → ", note.body or "")
    if found:
        stage = funnel.stage_named(stages, found.group(1).strip())
        if stage is not None and not stage.is_won and not stage.is_lost:
            return stage
    return (funnel.stage_named(stages, funnel.NEGOTIATION_NAME, "Negociação", "Negociacao") if lead.quoted_value else None) \
        or funnel.in_service_stage(stages)


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    apply_changes = "--aplicar" in sys.argv
    autostage.QUIET = True  # sem notas no meio da conversa e sem conversão pro Meta
    from scripts.etapas import review

    db = SessionLocal()
    counts = Counter()
    try:
        won_ids = [s.id for s in db.query(PipelineStage).filter(PipelineStage.is_won.is_(True))]
        ids = [lid for (lid,) in db.query(Lead.id).filter(
            Lead.stage_id.in_(won_ids), Lead.is_group.is_(False), Lead.deleted_at.is_(None))]
        print(f"\nConferindo {len(ids)} lead(s) em Ganho...\n")
        for lead_id in ids:
            lead = db.get(Lead, lead_id)
            stages = funnel._stages(db, lead.tenant_id)
            notes = auto_won_notes(db, lead, lead.stage.name)
            if not notes:
                counts["Ganho do vendedor (não mexe)"] += 1
                continue
            last = notes[-1]
            if lead.closed_at and abs(lead.closed_at - last.created_at) > SAME_TIME:
                counts["Ganho reconfirmado depois (não mexe)"] += 1
                continue
            if lead.receipt_status == "confirmed":
                counts["Comprovante conferido (continua Ganho)"] += 1
                continue
            proof = paid_proof(db, lead)
            if proof:
                counts["Pagamento na conversa (continua Ganho)"] += 1
                print(f"✓ {lead.name or lead.phone}: continua Ganho ({proof})")
                continue
            newer = db.query(Lead.id).filter(
                Lead.tenant_id == lead.tenant_id, Lead.contact_id == (lead.contact_id or lead.id),
                Lead.deal_number > lead.deal_number).first()
            if newer:
                counts["Tem negócio mais novo (conferir na mão)"] += 1
                print(f"? {lead.name or lead.phone}: Ganho sem prova, mas já tem negócio mais novo — conferir na mão")
                continue
            back = previous_stage(stages, last, lead)
            value = lead.deal_value
            for n in notes:
                db.delete(n)
            funnel.set_stage(lead, back)
            lead.deal_value = None
            lead.suggest_kind = ""
            db.flush()
            db.refresh(lead)  # lead.stage passa a ser a etapa nova (senão a releitura acha que ainda é Ganho)
            autostage.LOG.clear()
            final, why = review(db, lead, lead.created_at - datetime.timedelta(minutes=1), read_receipts=False)
            for obj in list(db.new):
                if isinstance(obj, Message) and obj.direction == "note":
                    db.expunge(obj)
            end = final.stage.name if final.stage else "—"
            counts[f"Ganho desfeito -> {end}"] += 1
            print(f"• {lead.name or lead.phone}: Ganho {_brl(value)} -> {end}" + (f" ({why[-1]})" if why and end != back.name else ""))
            if apply_changes:
                if end != back.name:
                    conv = db.query(Conversation).filter(Conversation.lead_id == final.id).order_by(Conversation.last_message_at.desc()).first()
                    if conv is not None:
                        db.add(Message(conversation_id=conv.id, direction="note",
                                       body=f"🤖 Revisão automática das etapas: {back.name} → {end}" + (f" ({why[-1]})" if why else "")))
                db.commit()
            else:
                db.rollback()
        print("\nResumo:")
        for label, n in counts.most_common():
            print(f"  {n} · {label}")
        if not apply_changes:
            print("\nNada foi mudado. Pra aplicar, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
