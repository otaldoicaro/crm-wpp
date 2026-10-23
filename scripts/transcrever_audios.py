"""🎙️ Transcreve os áudios antigos de UM cliente (os novos já são transcritos sozinhos quando chegam).

Só áudios dos últimos N dias (--dias, padrão 30) que têm a cópia guardada no CRM e ainda não têm
texto. O texto aparece embaixo do áudio e o 🔥 "quis comprar depois do preço" é conferido de novo
(as etapas NÃO mudam por áudio antigo). Sem --aplicar só mostra quantos são e o custo estimado.

    docker exec -it crm-app python -m scripts.transcrever_audios --cliente novaviseu
    (--so-negociacao-e-ganhos: só os áudios dos leads que estão hoje em Negociando ou Ganho)
    docker exec -d crm-app sh -c 'python -m scripts.transcrever_audios --cliente novaviseu --aplicar > /tmp/audios.log 2>&1'
    docker exec crm-app tail -n 5 /tmp/audios.log
"""

import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do CRM

from app.db import SessionLocal
from app.models import Conversation, Lead, Message, PipelineStage, Tenant
from app.services import buying, funnel, transcribe

PRICE_PER_HOUR = 0.04  # US$ (tabela do Groq, whisper-large-v3-turbo)


def _arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else default


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    key = _arg("--cliente")
    days = int(_arg("--dias", "30"))
    apply_changes = "--aplicar" in sys.argv
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter((Tenant.subdomain == key) | (Tenant.id == key)).first() if key else None
        if tenant is None:
            print("Diga o cliente: --cliente <identificador>. Clientes:", ", ".join(t.subdomain for t in db.query(Tenant)))
            return
        if not transcribe.is_enabled(tenant):
            print("⚠️ Transcrição desligada pra esse cliente: falta a chave do Groq no servidor (configurar_groq.sh) "
                  "ou ligar “🎙️ Transcrever os áudios” em ⚙ Configurações → Prazos e automações → Recursos.")
            return
        since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        query = (db.query(Message.id, Message.media_seconds)
                 .join(Conversation, Conversation.id == Message.conversation_id)
                 .filter(Conversation.tenant_id == tenant.id, Message.media_type == "audio", Message.transcript == "",
                         Message.media_stored_key != "", Message.created_at >= since))
        only_hot = "--so-negociacao-e-ganhos" in sys.argv
        if only_hot:  # só quem está em Negociando ou Ganho (onde o texto mais ajuda: 🔥 e o que vendeu)
            stages = db.query(PipelineStage).filter(PipelineStage.tenant_id == tenant.id).all()
            negotiation = funnel.stage_named(stages, funnel.NEGOTIATION_NAME, "Negociação", "Negociacao")
            ids = [s.id for s in stages if s.is_won] + ([negotiation.id] if negotiation else [])
            query = query.join(Lead, Lead.id == Conversation.lead_id).filter(Lead.stage_id.in_(ids))
        rows = query.order_by(Message.created_at).all()
        seconds = sum((s or 30) for _, s in rows)  # sem a duração guardada: conta 30 s
        print(f"\n{tenant.name}: {len(rows)} áudio(s) sem texto nos últimos {days} dias"
              + (" (só Negociando e Ganho)" if only_hot else "") + " "
              f"(~{seconds / 3600:.1f} h · custo estimado ~US$ {seconds / 3600 * PRICE_PER_HOUR:.2f}).")
        if not apply_changes:
            print("Nada foi transcrito. Pra transcrever, rode de novo com --aplicar no final (à noite).")
            return
        done = failed = hot = 0
        for i, (message_id, _) in enumerate(rows, 1):
            text = transcribe.check_message(message_id, rules=False)
            if text:
                done += 1
                message = db.get(Message, message_id)
                if message.direction == "in":
                    lead = db.get(Lead, db.get(Conversation, message.conversation_id).lead_id)
                    if lead is not None and buying.on_inbound(lead, text, message.created_at):
                        hot += 1
                        db.commit()
            else:
                failed += 1
            if i % 50 == 0:
                print(f"… {i} de {len(rows)} ({done} transcritos)")
            time.sleep(0.3)  # respiro pro servidor e pro limite do Groq
        print(f"\nResumo: {done} transcritos, {failed} sem texto (vazio, expirado ou erro), {hot} lead(s) ganharam o 🔥.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
