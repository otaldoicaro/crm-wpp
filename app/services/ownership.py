"""↪ Rastro de quem atende o lead: toda vez que o CRM (ou alguém) troca o vendedor, fica registrado
o de quem, pra quem e por quê, na conversa (nota que só a equipe vê) e no histórico do lead
(Lead.owner_trail, aparece na ficha). Assim o vendedor que "perdeu" o lead sabe o motivo.

Lead sem conversa ainda (ex: formulário): a troca fica guardada como pendente e vira nota assim que
a conversa existir (flush_pending, chamado quando chega ou sai a 1ª mensagem)."""

from __future__ import annotations

import datetime
from typing import Optional

from app.models import Conversation, Lead, Message, User
from app.timeutil import to_local

PENDING, NOTED = "P|", "N|"


def _name(db, user_id: Optional[str]) -> str:
    user = db.get(User, user_id) if user_id else None
    return user.name if user else "ninguém"


def changed(db, lead: Lead, previous_id: Optional[str], why: str, note: bool = True) -> None:
    """Chame DEPOIS de trocar lead.assigned_user_id. Não faz nada se o vendedor não mudou.
    note=False: só o histórico (quem chamou já deixou a nota dele na conversa)."""
    if previous_id == lead.assigned_user_id:
        return
    when = to_local(datetime.datetime.utcnow()).strftime("%d/%m %H:%M")
    text = f"{when} · {_name(db, previous_id)} → {_name(db, lead.assigned_user_id)} · {why}"
    conv = db.query(Conversation).filter(Conversation.lead_id == lead.id).order_by(Conversation.last_message_at.desc()).first() \
        if lead.id else None
    if conv is not None and note:
        db.add(Message(conversation_id=conv.id, direction="note", body=_note(text)))
    line = (NOTED if conv is not None else PENDING) + text
    lead.owner_trail = "\n".join(filter(None, [lead.owner_trail or "", line]))[-4000:]


def flush_pending(db, lead: Lead, conversation: Conversation) -> None:
    """Trocas registradas quando o lead ainda não tinha conversa: viram nota agora."""
    lines = (lead.owner_trail or "").splitlines()
    if not any(line.startswith(PENDING) for line in lines):
        return
    out = []
    for line in lines:
        if line.startswith(PENDING):
            db.add(Message(conversation_id=conversation.id, direction="note", body=_note(line[len(PENDING):])))
            line = NOTED + line[len(PENDING):]
        out.append(line)
    lead.owner_trail = "\n".join(out)


def _note(text: str) -> str:
    when, _, rest = text.partition(" · ")
    who, _, why = rest.partition(" · ")
    before, _, after = who.partition(" → ")
    return f"↪ O lead passou de {before} pra {after}: {why} ({when})"


def history(lead: Lead) -> list:
    """Linhas pra mostrar na ficha, da mais recente pra mais antiga."""
    return [line[2:] for line in reversed((lead.owner_trail or "").splitlines()) if line[:2] in (PENDING, NOTED)]
