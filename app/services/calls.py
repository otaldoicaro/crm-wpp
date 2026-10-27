"""📞 Ligações do WhatsApp no lead (todos os clientes).

De onde vêm:
- registro de ligação que o WhatsApp sincroniza com os aparelhos conectados (callLogMesssage),
  inclusive das ligações que o vendedor fez ou atendeu no celular: atendida ou não e a duração;
- evento CALL do Evolution: o cliente ligou pro WhatsApp do vendedor (tocando → atendida/não atendida).

Cada ligação fica marcada na mensagem (call_dir/call_outcome/call_seconds) e o lead guarda o resumo
(calls_total, calls_answered, calls_seconds, last_call_at) pro card do Pipeline e o topo da conversa."""

from __future__ import annotations

import datetime
from typing import Optional

from app.models import Conversation, Lead, Message

ANSWERED = "atendida"
OUTCOMES = {"CONNECTED": ANSWERED, "ACCEPTED_ELSEWHERE": ANSWERED, "MISSED": "não atendida", "REJECTED": "recusada",
            "FAILED": "não completou", "SILENCED_BY_DND": "não atendida", "SILENCED_UNKNOWN_CALLER": "não atendida"}
EVENT_OUTCOMES = {"accept": ANSWERED, "timeout": "não atendida", "reject": "recusada"}
SAME_CALL = datetime.timedelta(minutes=3)  # o evento CALL e o registro da mesma ligação


def parse_log(message: dict) -> Optional[dict]:
    """callLogMesssage (assim mesmo, com 3 "s") -> {"video", "outcome", "seconds"}; outro tipo -> None."""
    log = (message or {}).get("callLogMesssage")
    if not isinstance(log, dict):
        return None
    try:
        seconds = max(0, int(log.get("durationSecs") or 0))
    except (TypeError, ValueError):
        seconds = 0
    outcome = OUTCOMES.get(str(log.get("callOutcome") or "").upper(), "")
    return {"video": bool(log.get("isVideo")), "outcome": outcome or (ANSWERED if seconds else ""), "seconds": seconds}


def duration(seconds: int) -> str:
    """45 -> "45s"; 130 -> "2:10"; 3900 -> "1h05"."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def text(direction: str, info: dict, number_label: str = "") -> str:
    kind = "📹 Chamada de vídeo" if info.get("video") else "📞 Ligação"
    who = " do vendedor" if direction == "out" else " do cliente"
    parts = [p for p in (info.get("outcome"), duration(info["seconds"]) if info.get("seconds") else "") if p]
    return f"{kind}{who}" + (f" pelo WhatsApp {number_label}" if number_label else "") + ("".join(" · " + p for p in parts))


def mark(db, message: Message, direction: str, outcome: str, seconds: int = 0) -> None:
    message.call_dir, message.call_outcome, message.call_seconds = direction, outcome or "", int(seconds or 0)
    db.add(message)
    db.flush()
    conversation = db.get(Conversation, message.conversation_id)
    lead = db.get(Lead, conversation.lead_id) if conversation else None
    if lead is not None:
        refresh(db, lead)
    db.commit()


def refresh(db, lead: Lead) -> None:
    """Recalcula o resumo do lead a partir das ligações marcadas (dá pra rodar quantas vezes quiser)."""
    rows = (db.query(Message).join(Conversation, Conversation.id == Message.conversation_id)
            .filter(Conversation.lead_id == lead.id, Message.call_dir.in_(("in", "out")))
            .order_by(Message.created_at).all())
    logs = [m for m in rows if m.direction != "note"]
    calls = logs + [m for m in rows if m.direction == "note"  # evento CALL sem o registro da mesma ligação
                    and not any(abs(m.created_at - l.created_at) <= SAME_CALL for l in logs)]
    lead.calls_total = len(calls)
    lead.calls_answered = sum(1 for m in calls if m.call_outcome == ANSWERED)
    lead.calls_seconds = sum(m.call_seconds or 0 for m in calls)
    lead.last_call_at = max((m.created_at for m in calls), default=None)
    db.add(lead)
