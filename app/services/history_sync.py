"""Recupera mensagens que o CRM não registrou, a partir do banco do próprio Evolution (que guarda
tudo o que passou pelo WhatsApp conectado). Acontecia principalmente com conversas endereçadas
por LID (o id interno que o WhatsApp está usando no lugar do telefone): a mensagem chegava sem
o telefone, o CRM não achava o lead e descartava.

- Importa só o que ainda não está no CRM (pelo id da mensagem): rodar de novo nunca duplica.
- Mantém a hora original, na ordem em que aconteceu.
- Mensagem nossa (do celular) só entra se o contato já é lead, igual no dia a dia (conversa
  pessoal não vira lead). Mensagem do cliente cria o lead se ele não existir.
- Roda sozinho a cada 6h olhando os últimos 2 dias; dá pra rodar à mão pra trás
  (scripts/recuperar_historico.py)."""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Optional

from app.db import SessionLocal
from app.models import WhatsAppNumber
from app.services import evolution_client

logger = logging.getLogger("history_sync")

INTERVAL_SECONDS = 6 * 60 * 60
AUTO_DAYS = 2
MAX_PAGES = 100  # 100 x 200 = 20 mil mensagens por número por rodada
SKIP_TYPES = {"protocolMessage", "reactionMessage", "pollUpdateMessage", "senderKeyDistributionMessage"}


def _records(instance: str, since: datetime.datetime, until: datetime.datetime) -> list:
    out, page = [], 1
    since_iso, until_iso = since.isoformat() + "Z", until.isoformat() + "Z"
    while page <= MAX_PAGES:
        chunk = evolution_client.find_messages(instance, since_iso, until_iso, page=page)
        records = chunk.get("records") or []
        out.extend(records)
        if not records or page >= int(chunk.get("pages") or 1):
            break
        page += 1
    return out


def sync_number(number_id: str, days: int, apply: bool) -> dict:
    """Devolve {"vistas", "ja_no_crm", "importar_cliente", "importar_nossas", "ignoradas",
    "novos_leads", "leads": {nome: quantidade}}."""
    from app.routers.webhooks_evolution import _parse_content, _phone_from_key
    from app.services.inbound import ingest_inbound, message_exists, record_outbound_from_phone
    from app.services.lead_match import find_lead_by_phone

    stats = {"vistas": 0, "ja_no_crm": 0, "importar_cliente": 0, "importar_nossas": 0, "ignoradas": 0,
             "novos_leads": 0, "leads": {}}
    db = SessionLocal()
    try:
        number = db.get(WhatsAppNumber, number_id)
        if number is None or number.provider != "evolution":
            return stats
        until = datetime.datetime.utcnow()
        since = until - datetime.timedelta(days=days)
        records = _records(number.evolution_instance, since, until)
        records.sort(key=lambda r: int(r.get("messageTimestamp") or 0))
        for rec in records:
            key = rec.get("key") or {}
            jid = key.get("remoteJid") or ""
            stats["vistas"] += 1
            if (not jid or jid.endswith(("@broadcast", "@newsletter", "@g.us")) or not key.get("id")
                    or rec.get("messageType") in SKIP_TYPES):
                stats["ignoradas"] += 1
                continue
            if message_exists(db, key["id"]):
                stats["ja_no_crm"] += 1
                continue
            body, media_type, _ = _parse_content(rec.get("message") or {})
            if not body and not media_type:
                stats["ignoradas"] += 1
                continue
            phone = _phone_from_key(key, rec, db, number)
            when = datetime.datetime.utcfromtimestamp(int(rec.get("messageTimestamp") or 0)) if rec.get("messageTimestamp") else None
            lead = find_lead_by_phone(db, number.tenant_id, phone)
            from_me = bool(key.get("fromMe"))
            if from_me and lead is None:
                stats["ignoradas"] += 1  # conversa nossa com quem não é lead (pessoal): fica de fora
                continue
            name = (lead.name if lead else rec.get("pushName")) or phone
            stats["importar_nossas" if from_me else "importar_cliente"] += 1
            stats["leads"][name] = stats["leads"].get(name, 0) + 1
            if lead is None:
                stats["novos_leads"] += 1
            if not apply:
                continue
            media_id = key["id"] if media_type else ""
            if from_me:
                record_outbound_from_phone(db, number, phone, key["id"], body, media_id, media_type, when=when)
            else:
                ingest_inbound(db, number, from_phone=phone, wa_message_id=key["id"], body=body, media_id=media_id,
                               media_type=media_type, profile_name=rec.get("pushName") or "", when=when)
        return stats
    finally:
        db.close()


def sync_all(days: int, apply: bool) -> dict:
    """{nome do número: stats} de todos os WhatsApps ativos do Evolution."""
    db = SessionLocal()
    try:
        numbers = [(n.id, n.label) for n in db.query(WhatsAppNumber).filter(
            WhatsAppNumber.provider == "evolution", WhatsAppNumber.is_active.is_(True))]
    finally:
        db.close()
    result = {}
    for number_id, label in numbers:
        try:
            result[label] = sync_number(number_id, days, apply)
        except evolution_client.EvolutionError as exc:
            result[label] = {"erro": str(exc)}
        except Exception as exc:  # um número com problema não para os outros
            logger.exception("history_sync: falhou no número %s", label)
            result[label] = {"erro": str(exc)}
    return result


def schedule(delay_seconds: int = 300) -> None:
    if not evolution_client.is_configured():
        return

    def run():
        try:
            result = sync_all(AUTO_DAYS, apply=True)
            imported = sum(r.get("importar_cliente", 0) + r.get("importar_nossas", 0) for r in result.values())
            if imported:
                logger.warning("history_sync: %s mensagem(ns) que tinham escapado foram recuperadas", imported)
        except Exception:
            logger.exception("history_sync: falhou")
        schedule(INTERVAL_SECONDS)

    timer = threading.Timer(delay_seconds, run)
    timer.daemon = True
    timer.start()
