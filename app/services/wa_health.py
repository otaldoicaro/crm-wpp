"""🩺 O CRM cuida sozinho dos WhatsApps conectados (Evolution), sem ninguém rodar comando no servidor.

1. Conexão "zumbi": o Evolution diz que está conectado, mas o envio volta "Connection Closed".
   - No envio (messaging): reinicia a conexão daquele número e tenta mandar de novo, uma vez.
   - A cada CHECK_SECONDS: testa cada número conectado com uma consulta que não manda nada
     (o próprio número tem WhatsApp?). Se voltar "Connection Closed", reinicia a conexão.
2. Caiu (webhook ou vigia): tenta religar sozinho primeiro; só se não voltar avisa na tela o vendedor
   dono e os admins (aí precisa de QR code), uma vez por queda.
4. Voltou (religou sozinho ou alguém leu o QR code): puxa na hora do Evolution as mensagens do período
   que não entraram no CRM (catch_up).
3. Parou de receber (o WhatsApp não entrega mais pro servidor, mas a conexão parece boa): no horário
   de atendimento, número que costuma receber bastante e está há SILENT_AFTER sem nenhuma mensagem de
   cliente -> reinicia a conexão (no máximo a cada SILENT_COOLDOWN) e avisa os admins.
Toda reinicialização respeita HEAL_COOLDOWN por número: nunca fica reiniciando em loop.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time

from app.db import SessionLocal
from app.models import Conversation, Message, Tenant, User, WhatsAppNumber
from app.services import evolution_client

logger = logging.getLogger("wa_health")

CHECK_SECONDS = 5 * 60
HEAL_COOLDOWN = 10 * 60
SILENT_AFTER = datetime.timedelta(minutes=90)
SILENT_COOLDOWN = 3 * 60 * 60
SILENT_MIN_PER_HOUR = 3  # só número que recebe, em média, 3+ mensagens por hora de atendimento
WAIT_OPEN_SECONDS = 30
WARN_EVERY = 60 * 60  # o aviso "travou" do mesmo número no máximo 1 vez por hora

_lock = threading.Lock()
_healed: dict = {}  # instância -> quando reiniciou (monotonic)
_silent: dict = {}  # instância -> quando reiniciou por "parou de receber"
_warned: dict = {}  # instância -> quando avisou "travou"


def is_zombie_error(exc: Exception) -> bool:
    """"Connection Closed" (no envio vem assim; na consulta vem como statusCode 428 do Baileys)."""
    text = str(exc).lower()
    return "connection closed" in text or "'statuscode': 428" in text or "precondition required" in text


def _works(instance: str, phone: str) -> bool:
    """A conexão funciona de verdade? ("open" não basta: o zumbi também diz open)"""
    try:
        evolution_client.whatsapp_numbers(instance, [phone])
        return True
    except evolution_client.EvolutionError:
        return False


def heal(number: WhatsAppNumber, why: str):
    """Reinicia a conexão do número e espera voltar funcionando. True = voltou (vale tentar de novo),
    False = não voltou (precisa de QR code), None = já reiniciou há pouco (HEAL_COOLDOWN): não fez nada."""
    instance = number.evolution_instance
    now = time.monotonic()
    with _lock:
        if now - _healed.get(instance, -HEAL_COOLDOWN) < HEAL_COOLDOWN:
            return None
        _healed[instance] = now
    logger.warning("wa_health: reiniciando a conexão de %s (%s)", number.label, why)
    try:
        evolution_client.restart(instance)
    except evolution_client.EvolutionError as exc:
        logger.warning("wa_health: não deu pra reiniciar %s: %s", number.label, exc)
        return False
    deadline = time.monotonic() + WAIT_OPEN_SECONDS
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            if evolution_client.connection_state(instance) == "open" and _works(instance, number.phone_number):
                return True
        except evolution_client.EvolutionError:
            pass
    return False


def _admins_and_owner(db, number: WhatsAppNumber) -> list:
    ids = [u.id for u in db.query(User).filter(User.tenant_id == number.tenant_id, User.role == "admin",
                                               User.is_active.is_(True), User.removed_at.is_(None))]
    if number.owner_user_id and number.owner_user_id not in ids:
        ids.append(number.owner_user_id)
    return ids


def _warn(db, number: WhatsAppNumber, user_ids: list, title: str, body: str) -> None:
    from app.services import notify

    for user_id in user_ids:
        notify.push(db, number.tenant_id, user_id, "whatsapp", title, body, "/whatsapp")


def on_dropped(db, number: WhatsAppNumber) -> None:
    """O número estava conectado e caiu (precisa de QR code): avisa o dono e os admins (sem commit)."""
    _warn(db, number, _admins_and_owner(db, number), f"📵 {number.label} desconectou",
          "As mensagens desse número não chegam nem saem pelo CRM. Reconecte em Meu WhatsApp → Conectar agora (QR code).")


def recover(db, number: WhatsAppNumber) -> bool:
    """O número caiu: tenta religar sozinho (sessão salva, sem QR). Voltou: puxa as mensagens do período.
    Não voltou: avisa o vendedor e os admins (sem commit). True = voltou."""
    if heal(number, "caiu") is True:
        number.connection_state = "open"
        catch_up(number.id)
        return True
    on_dropped(db, number)
    return False


def dropped_in_background(number_id: str) -> None:
    """Webhook disse que caiu: tenta religar fora do webhook (leva até ~30 s). Se o próprio CRM está
    reiniciando esse número agora, a queda é dele mesmo: não faz nada."""
    def go():
        db = SessionLocal()
        try:
            number = db.get(WhatsAppNumber, number_id)
            if number is None or time.monotonic() - _healed.get(number.evolution_instance, -HEAL_COOLDOWN) < 120:
                return
            recover(db, number)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("wa_health: falhou ao religar %s", number_id)
        finally:
            db.close()

    threading.Thread(target=go, daemon=True).start()


_caught: dict = {}  # número -> quando puxou o histórico pela última vez
CATCH_UP_DAYS = 1


def catch_up(number_id: str) -> None:
    """O número voltou (religou sozinho ou leu o QR code): puxa já do Evolution as mensagens que não
    entraram no CRM (o WhatsApp entrega o que ficou parado assim que reconecta). Em segundo plano,
    no máximo 1 vez a cada 2 min por número."""
    from app.services import history_sync

    with _lock:
        if time.monotonic() - _caught.get(number_id, -120) < 120:
            return
        _caught[number_id] = time.monotonic()

    def go():
        time.sleep(30)  # dá tempo do WhatsApp entregar o que estava parado
        try:
            st = history_sync.sync_number(number_id, CATCH_UP_DAYS, apply=True)
            got = st.get("importar_cliente", 0) + st.get("importar_nossas", 0)
            if got:
                logger.warning("wa_health: %s mensagem(ns) do período sem conexão recuperadas (número %s)", got, number_id)
        except Exception:
            logger.exception("wa_health: falhou ao puxar o histórico do número %s", number_id)

    threading.Thread(target=go, daemon=True).start()


def _receives_a_lot(db, number: WhatsAppNumber, hours: dict, now: datetime.datetime) -> bool:
    week = now - datetime.timedelta(days=7)
    count = (db.query(Message.id).join(Conversation, Conversation.id == Message.conversation_id)
             .filter(Conversation.whatsapp_number_id == number.id, Message.direction == "in", Message.created_at >= week)
             .count())
    open_hours = sum((end - start) / 60 for start, end in hours.values()) or 1
    return count / open_hours >= SILENT_MIN_PER_HOUR


def _last_inbound(db, number: WhatsAppNumber):
    row = (db.query(Message.created_at).join(Conversation, Conversation.id == Message.conversation_id)
           .filter(Conversation.whatsapp_number_id == number.id, Message.direction == "in")
           .order_by(Message.created_at.desc()).first())
    return row[0] if row else None


def check_number(db, number: WhatsAppNumber, now: datetime.datetime) -> str:
    """Confere um número. Devolve o que fez ("", "caiu", "religou", "zumbi", "silencioso")."""
    from app.services.after_hours import is_open
    from app.services.response_times import parse_hours

    try:
        state = evolution_client.connection_state(number.evolution_instance)
    except evolution_client.EvolutionError:
        return ""  # Evolution fora do ar ou instância apagada: a tela WhatsApp já mostra
    if state != "open":
        if number.connection_state == "open" and state == "close":
            number.connection_state = "close"
            return "religou" if recover(db, number) else "caiu"
        return ""
    if number.connection_state != "open":
        number.connection_state = "open"  # voltou (ex: depois de reiniciar o Evolution)
    try:  # consulta que precisa da conexão de verdade, sem mandar nada pra ninguém
        evolution_client.whatsapp_numbers(number.evolution_instance, [number.phone_number])
    except evolution_client.EvolutionError as exc:
        if is_zombie_error(exc):
            ok = heal(number, "conexão zumbi")
            if ok:
                catch_up(number.id)
            with _lock:
                warn = ok is False and time.monotonic() - _warned.get(number.evolution_instance, -WARN_EVERY) >= WARN_EVERY
                if warn:
                    _warned[number.evolution_instance] = time.monotonic()
            if warn:
                logger.warning("wa_health: %s continua travado depois de reiniciar: precisa de QR code", number.label)
                _warn(db, number, _admins_and_owner(db, number), f"📵 {number.label} travou",
                      "O CRM tentou religar sozinho e não conseguiu. Desconecte e conecte de novo em Meu WhatsApp (QR code).")
            return "zumbi"
        return ""
    tenant = db.get(Tenant, number.tenant_id)
    hours = parse_hours(tenant.business_hours if tenant else "")
    last = _last_inbound(db, number)
    if (tenant is not None and is_open(tenant, now) and is_open(tenant, now - SILENT_AFTER)
            and last is not None and now - last >= SILENT_AFTER and _receives_a_lot(db, number, hours, now)):
        instance = number.evolution_instance
        with _lock:
            if time.monotonic() - _silent.get(instance, -SILENT_COOLDOWN) < SILENT_COOLDOWN:
                return ""
            _silent[instance] = time.monotonic()
        heal(number, "sem receber mensagem de cliente há muito tempo")
        admins = [i for i in _admins_and_owner(db, number) if i != number.owner_user_id]
        _warn(db, number, admins, f"🩺 {number.label} estava sem receber mensagens",
              "O CRM religou a conexão sozinho. Se o vendedor notar mensagem no celular que não aparece no CRM, "
              "desconecte e conecte de novo em Meu WhatsApp (QR code).")
        return "silencioso"
    return ""


def run() -> dict:
    db = SessionLocal()
    done = {}
    try:
        now = datetime.datetime.utcnow()
        numbers = db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == "evolution",
                                                  WhatsAppNumber.is_active.is_(True)).all()
        for number in numbers:
            try:
                what = check_number(db, number, now)
                db.commit()
                if what:
                    done[number.label] = what
            except Exception:
                db.rollback()
                logger.exception("wa_health: falhou em %s", number.label)
    finally:
        db.close()
    return done


def schedule(delay_seconds: int = 120) -> None:
    if not evolution_client.is_configured():
        return

    def go():
        try:
            done = run()
            if done:
                logger.warning("wa_health: %s", done)
        except Exception:
            logger.exception("wa_health: falhou")
        schedule(CHECK_SECONDS)

    timer = threading.Timer(delay_seconds, go)
    timer.daemon = True
    timer.start()
