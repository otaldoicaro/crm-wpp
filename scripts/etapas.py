"""🤖 Revisão em massa das etapas: refaz, conversa por conversa e na ordem em que aconteceu, a
passagem automática de etapa (a mesma que agora roda sozinha a cada mensagem):

  time respondeu -> Em atendimento · cliente passou carro/perguntou preço -> Qualificado ·
  time mandou preço -> Negociando · comprovante conferido / venda fechada -> Ganho ·
  perda clara -> Perdido · pediu de novo depois de perdido -> Novo

- Lead que o vendedor já fechou (Ganho/Perdido) não é desfeito; só volta pro atendimento se o
  cliente pediu algo novo DEPOIS de fechar.
- Comprovantes (fotos/PDFs do cliente) são lidos de novo com a leitura conferida (3 leituras +
  preço enviado). Valor sem certeza não fecha sozinho: vira o aviso "confirmar venda".
- Não manda conversão pro Meta/Google (são vendas antigas).

Rodar no terminal do VPS:
    docker exec -it crm-app python -m scripts.etapas              # últimos 60 dias, só mostra
    docker exec -it crm-app python -m scripts.etapas 30           # últimos 30 dias
    docker exec -it crm-app python -m scripts.etapas 60 --aplicar # aplica
    (--sem-comprovantes: não lê as imagens, bem mais rápido)
    (--so-fechamentos: só mexe em quem termina em Ganho ou Perdido; o resto fica como está)

Em segundo plano (não depende do console ficar aberto; o andamento vai pra um arquivo):
    docker exec -d crm-app sh -c 'python -m scripts.etapas 60 --so-fechamentos --aplicar > /tmp/etapas.log 2>&1'
    docker exec crm-app tail -n 30 /tmp/etapas.log
"""

import datetime
import os
import time
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, WhatsAppNumber
from app.services import autostage, followup, funnel, receipt_text, receipts, suggestions
from app.services import media_store, messaging


PAUSE = 0.05  # segundos entre leads (10 mil leads ≈ +8 min, mas sem travar o sistema)


def _brl(v) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if v else ""


def _check_receipt(db, lead: Lead, message: Message, quoted: list) -> str:
    """Lê o comprovante; fecha sozinho se o valor estiver conferido. Devolve o que fez ("" = nada)."""
    if not receipts.worth_checking(lead):
        return ""
    # só a cópia que o CRM já guardou: buscar no Evolution cada imagem antiga pesa no banco dele
    # (e no WhatsApp de todo mundo). Sem cópia, fica de fora
    content, mime = media_store.load(message.media_stored_key) if message.media_stored_key else (None, None)
    if not content:
        return ""
    result = receipt_text.read_receipt(content, mime, quoted)
    amount = float(result.get("amount") or 0)
    if not result.get("is_payment_receipt") or result.get("confidence") == "baixa" or amount <= 0:
        return ""
    lead.receipt_message_id, lead.receipt_amount = message.id, amount
    lead.receipt_info = receipts.describe(result)
    lead.receipt_candidates = ",".join(str(v) for v in result.get("candidates", [])[:4])[:120]
    if result.get("confidence") == "alta" and autostage.apply(
        db, lead, "ganho", amount=amount, why="comprovante de pagamento"
    ):
        return f"comprovante conferido {_brl(amount)}"
    lead.receipt_status = "pending"
    return f"comprovante pra confirmar ({_brl(amount)}, valores lidos: {', '.join(_brl(v) for v in result.get('candidates', []))})"


def review(db, lead: Lead, since: datetime.datetime, read_receipts: bool) -> tuple:
    """Refaz as etapas de um lead. Devolve (lead final, motivos)."""
    msgs = (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .filter(Conversation.lead_id == lead.id, Message.direction.in_(["in", "out"]), Message.created_at >= since)
        .order_by(Message.created_at)
        .all()
    )
    stage = lead.stage
    closed_by_hand_at = lead.closed_at if stage and (stage.is_won or stage.is_lost) else None
    quoted, why = [], []
    for m in msgs:
        if closed_by_hand_at and m.created_at <= closed_by_hand_at:
            continue  # fechado pelo vendedor: só conta o que veio depois
        if lead.contact_id and lead.created_at and m.created_at < lead.created_at - datetime.timedelta(minutes=1):
            continue  # negócio novo (recompra): as mensagens de antes são do negócio anterior
        body = m.body or ""
        before = lead.stage.name if lead.stage else ""
        log_start = len(autostage.LOG)
        if m.direction == "in":
            if lead.stage and (lead.stage.is_won or lead.stage.is_lost):
                new = autostage.reopen_if_request(db, lead, body, m.created_at)
                if new is not lead or (lead.stage and not lead.stage.is_lost and not lead.stage.is_won):
                    why.append(f"pediu de novo depois de fechar: “{body[:60]}”")
                lead = new
            funnel.on_message(db, lead, outbound=False)
            followup.on_inbound_text(db, lead, body)
            suggestions.on_message(lead, body, outbound=False)
            if read_receipts and m.media_type in ("image", "document") and (quoted or lead.quoted_value):
                did = _check_receipt(db, lead, m, quoted)
                if did:
                    why.append(did)
        else:
            autostage.refund_if_asked(db, lead, body, outbound=True)
            quoted.extend(followup.find_prices(body))
            funnel.on_message(db, lead, outbound=True)
            followup.on_outbound_text(db, lead, body)
            suggestions.on_message(lead, body, outbound=True)
        db.flush()
        db.refresh(lead)
        after = lead.stage.name if lead.stage else ""
        if after != before and not any(after in w for w in why[-1:]):
            reason = next((t for lid, t in reversed(autostage.LOG[log_start:]) if lid == lead.id), "")
            why.append(reason.replace("🤖 Etapa automática: ", "") if reason else f"{after}: “{body[:60]}”")
    return lead, why


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # andamento aparece no log na hora
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 60
    apply_changes = "--aplicar" in sys.argv
    read_receipts = "--sem-comprovantes" not in sys.argv
    only_closing = "--so-fechamentos" in sys.argv  # só Ganho/Perdido; quem está no meio do funil fica como está
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    autostage.QUIET = True
    if read_receipts and not receipt_text.ocr_available():
        print("⚠️  Leitor de imagens (Tesseract) não instalado: comprovantes ficam de fora.")
        read_receipts = False
    db = SessionLocal()
    moves = Counter()
    try:
        ids = [lid for (lid,) in db.query(Lead.id).join(Conversation, Conversation.lead_id == Lead.id).filter(
            Lead.is_group.is_(False), Lead.deleted_at.is_(None), Lead.archived_at.is_(None), Lead.tag != "outro",
            Conversation.last_message_at >= since,
        ).distinct()]
        print(f"\nRevisando {len(ids)} lead(s) com conversa nos últimos {days} dia(s)"
              + (" — só fechamentos (Ganho/Perdido)" if only_closing else "") + "...\n")
        started = time.time()
        for done_count, lead_id in enumerate(ids, 1):
            if done_count % 500 == 0:
                mins = (time.time() - started) / 60
                left = mins / done_count * (len(ids) - done_count)
                print(f"… {done_count} de {len(ids)} leads revisados ({mins:.0f} min até agora, faltam uns {left:.0f} min)")
            lead = db.get(Lead, lead_id)
            start = lead.stage.name if lead.stage else "—"
            if only_closing and lead.stage and (lead.stage.is_won or lead.stage.is_lost):
                continue  # já fechado: não mexe (nem reabre pra Novo)
            autostage.LOG.clear()
            final, why = review(db, lead, since, read_receipts)
            time.sleep(PAUSE)  # respiro entre um lead e outro: o CRM continua atendendo quem está usando
            end = final.stage.name if final.stage else "—"
            if only_closing and not (final.stage and (final.stage.is_won or final.stage.is_lost)):
                for obj in list(db.new):
                    if isinstance(obj, Message) and obj.direction == "note":
                        db.expunge(obj)
                db.rollback()
                continue
            # nenhuma nota no meio da conversa: só um resumo no fim
            for obj in list(db.new):
                if isinstance(obj, Message) and obj.direction == "note":
                    db.expunge(obj)
            if end != start or final is not lead or why:
                if end == start and final is lead and not why:
                    db.rollback()
                    continue
                moves[end] += 1
                value = f" · {_brl(final.deal_value)}" if final.stage and final.stage.is_won and final.deal_value else ""
                print(f"• {lead.name or lead.phone}: {start} -> {end}{value}")
                for w in why[-4:]:
                    print(f"     {w}")
                if apply_changes:
                    conv = db.query(Conversation).filter(Conversation.lead_id == final.id).order_by(Conversation.last_message_at.desc()).first()
                    if conv is not None:
                        db.add(Message(conversation_id=conv.id, direction="note",
                                       body=f"🤖 Revisão automática das etapas: {start} → {end}{value}" + (f" ({why[-1]})" if why else "")))
                    db.commit()
                else:
                    db.rollback()
            else:
                db.rollback()
        # negociação parada há 7+ dias -> Perdido (motivo claro preenchido; senão "⚠️ motivo a preencher")
        silent = autostage.lose_silent_leads(db, commit=False)
        if silent:
            print(f"\nSem nenhuma mensagem há {autostage.LOST_AFTER_DAYS}+ dias (vão pra Perdido):")
            for lead, reason in silent:
                print(f"• {lead.name or lead.phone}: -> Perdido · {reason or '⚠️ motivo a preencher'}")
                moves["Perdido (parado)"] += 1
        for obj in list(db.new):
            if isinstance(obj, Message) and obj.direction == "note":
                db.expunge(obj)
        if apply_changes:
            for lead, reason in silent:
                conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first()
                if conv is not None:
                    db.add(Message(conversation_id=conv.id, direction="note", body="🤖 Revisão automática das etapas: Perdido — sem mensagens há "
                                   f"{autostage.LOST_AFTER_DAYS}+ dias" + (f" ({reason})" if reason else ". ⚠️ Preencha o motivo da perda.")))
            db.commit()
        else:
            db.rollback()
        print("\nResumo:", ", ".join(f"{n} -> {stage}" for stage, n in moves.most_common()) or "nada pra mudar")
        if moves and not apply_changes:
            print("Pra aplicar, rode de novo com --aplicar no final.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
