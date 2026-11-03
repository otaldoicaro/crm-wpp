"""🩺 O CRM cuida sozinho dos WhatsApps conectados (Evolution), sem ninguém rodar comando no servidor.

1. Conexão "zumbi": o Evolution diz que está conectado, mas o envio volta "Connection Closed".
   - No envio (messaging): reinicia a conexão daquele número e tenta mandar de novo, uma vez.
   - A cada CHECK_SECONDS: testa cada número conectado com uma consulta que não manda nada
     (o próprio número tem WhatsApp?). Se voltar "Connection Closed", reinicia a conexão.
2. Caiu ou travou (webhook ou vigia): tenta religar sozinho (reiniciar); só se não voltar avisa na tela o
   vendedor dono e os admins pra Desconectar e Conectar (QR code). O vigia NUNCA cria sessão nova sozinho:
   só o Desconectar/Conectar de alguém.
4. Voltou (religou sozinho ou alguém leu o QR code): puxa na hora do Evolution as mensagens do período
   que não entraram no CRM (catch_up).
3. Parou de receber (o WhatsApp não entrega mais pro servidor, mas a conexão parece boa e passa no teste):
   no horário de atendimento, número que costuma receber bastante, sem nenhuma mensagem de cliente por um
   tempo que depende do movimento dele (_silent_after: 10 min se recebe muito) enquanto os outros recebem -> reinicia; continuou parado -> avisa o vendedor e os
   admins pra reconectar (QR).
Toda reinicialização respeita HEAL_COOLDOWN por número: nunca fica reiniciando em loop.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import secrets
import threading
import time

from app.db import SessionLocal
from app.models import Conversation, Message, Tenant, User, WhatsAppNumber
from app.services import evolution_client

logger = logging.getLogger("wa_health")

CHECK_SECONDS = 60  # cada rodada: 2 consultas leves por número + 1 lista das sessões
HEAL_COOLDOWN = 10 * 60
# "parou de receber": o tempo sem mensagem que já é estranho depende do movimento de cada WhatsApp
# (4x o intervalo médio entre mensagens no horário de atendimento), entre SILENT_MIN e SILENT_MAX.
# Ex: 30 mensagens/hora -> 10 min; 6/hora -> 40 min. Quem recebe menos de SILENT_MIN_PER_HOUR fica de fora.
SILENT_MIN = datetime.timedelta(minutes=10)
SILENT_MAX = datetime.timedelta(minutes=90)
SILENT_GAPS = 4
SILENT_MIN_PER_HOUR = 3  # só número que recebe, em média, 3+ mensagens por hora de atendimento
WAIT_OPEN_SECONDS = 30
# 🧘 modo calmo: o CRM nunca reinicia a conexão sozinho (cada reinício podia derrubar o número de vez ou
# cortar o QR code no meio da leitura). Ele só confere, puxa as mensagens que escaparam e avisa.
AUTO_RESTART = False
RECONNECT_TEXT = ("Entre em Meu WhatsApp → Desconectar, depois Conectar agora e leia o QR code com o celular.")
RESET_KEEP = datetime.timedelta(hours=12)  # depois de um Desconectar, a sessão antiga não volta: espera o QR
WARN_EVERY = 60 * 60  # o aviso "travou" do mesmo número no máximo 1 vez por hora (e só de novo depois de voltar)
NIGHT_FILE = "/tmp/evolution-renovando.json"  # scripts/evolution_noite.py: Evolution reiniciando de madrugada
NIGHT_PAUSE = 15 * 60  # o vigia não mexe em nada enquanto isso (no máximo 15 min, se o script morrer no meio)

_lock = threading.Lock()
_healed: dict = {}  # instância -> quando reiniciou (monotonic)
_silent: dict = {}  # instância -> quando reiniciou por "parou de receber"
_silent_strikes: dict = {}  # instância -> quantas vezes seguidas reiniciou e continuou sem receber
_warned: dict = {}  # instância -> quando avisou "travou" (sai daqui quando volta a funcionar)
_silent_warned: set = set()  # instância que já teve o aviso "parou de receber" (sai quando chega mensagem)
_imported: dict = {}  # sessão antiga -> quando puxou as mensagens dela e tentou apagar
EXTRA_RETRY = 10 * 60  # sessão antiga: puxa as mensagens dela (e tenta apagar a desconectada) a cada 10 min


def is_zombie_error(exc: Exception) -> bool:
    """"Connection Closed" (no envio vem assim; na consulta vem como statusCode 428 do Baileys)."""
    text = str(exc).lower()
    return "connection closed" in text or "'statuscode': 428" in text or "precondition required" in text


def _works(instance: str, phone: str) -> bool:
    """A conexão funciona de verdade? ("open" não basta: o zumbi também diz open)"""
    if not phone:
        return True  # sem o número não dá pra testar: confia no "open"
    try:
        evolution_client.whatsapp_numbers(instance, [phone])
        return True
    except evolution_client.EvolutionTimeout:
        return True  # lento não é travado
    except evolution_client.EvolutionError as exc:
        return not is_zombie_error(exc)  # só "conexão fechada" conta como travado


def heal(number: WhatsAppNumber, why: str):
    """Reinicia a conexão do número e espera voltar funcionando. True = voltou (vale tentar de novo),
    False = não voltou (precisa de QR code), None = já reiniciou há pouco (HEAL_COOLDOWN): não fez nada."""
    if not AUTO_RESTART:
        return False
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


def fresh_instance_name(db, number: WhatsAppNumber) -> str:
    """Nome novo, nunca usado, pra mesma linha do CRM: "<nome>-r<4 letras>" (ex: novaviseu-14-r3fa9).
    Troca o sufixo de uma limpeza anterior em vez de empilhar."""
    root = re.sub(r"-r[0-9a-f]{4}$", "", number.evolution_instance or "crm")
    taken = {name for (name,) in db.query(WhatsAppNumber.evolution_instance).all()}
    while True:
        name = f"{root}-r{secrets.token_hex(2)}"
        if name not in taken:
            return name


def hard_reset(db, number: WhatsAppNumber) -> None:
    """Desconecta 100%: o número passa a usar uma sessão NOVA no Evolution (outro nome de instância, sem
    nada salvo), então o "Conectar agora" sempre mostra o QR code. Apagar a sessão velha não basta: na
    conexão zumbi o Evolution recusa sair/apagar e, ao religar, carrega a sessão velha do banco dele.
    A velha é largada (tenta sair/apagar; o que vier dela é ignorado: o CRM não conhece mais o nome).
    As conversas do CRM não mudam: continuam na mesma linha do número (commit aqui)."""
    old = number.evolution_instance
    with _lock:
        _healed[old] = time.monotonic()  # a queda que vem pelo webhook é nossa: não tenta religar
    number.evolution_instance = fresh_instance_name(db, number)
    number.connection_state = "close"
    number.reset_at = datetime.datetime.utcnow()
    db.add(number)
    db.commit()
    for action in (evolution_client.logout, evolution_client.delete_instance):
        try:
            action(old)
        except evolution_client.EvolutionError:
            pass  # zumbi não sai direito / já apagada: segue
    try:
        evolution_client.create_instance(number.evolution_instance)  # já com o webhook
    except evolution_client.EvolutionError as exc:
        logger.warning("wa_health: não deu pra criar %s: %s", number.evolution_instance, exc)  # o "Conectar agora" cria
    logger.warning("wa_health: %s desconectado 100%% (sessão nova %s no lugar de %s); precisa ler o QR code",
                   number.label, number.evolution_instance, old)


def paused() -> bool:
    """O Evolution está sendo renovado de madrugada: queda e "zumbi" agora são esperados; quem confere
    cada número depois é o próprio script (scripts/evolution_noite.py)."""
    try:
        return time.time() - os.path.getmtime(NIGHT_FILE) < NIGHT_PAUSE
    except OSError:
        return False


def instance_root(name: str) -> str:
    return re.sub(r"-r[0-9a-f]{4}$", "", name or "")


def owner_of_old_session(db, instance: str, event: str, state: str):
    """Evento de uma sessão que o CRM largou no "Desconectar 100%" (ex: novaviseu-14 quando o número já
    está em novaviseu-14-r3fa9, ou o contrário). Se chegou mensagem por ela e a atual do número não está
    conectada, o número volta a usar ela (e puxa o que chegou nesse meio-tempo); se as duas estão
    conectadas, as mensagens dela entram mesmo assim (o id da mensagem evita duplicar). Nunca descarta
    mensagem de cliente. Devolve o número (ou None)."""
    root = instance_root(instance)
    number = next((n for n in db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == "evolution",
                                                               WhatsAppNumber.is_active.is_(True))
                   if instance_root(n.evolution_instance) == root), None) if root else None
    if number is None:
        return None
    # só mensagem prova que ela funciona: o zumbi também manda "open" (e aí o Conectar voltaria a pular o QR)
    alive = event != "connection.update"
    if alive and number.connection_state != "open" and not _just_reset(number):
        logger.warning("wa_health: %s voltou a usar a sessão %s (a %s não está conectada)",
                       number.label, instance, number.evolution_instance)
        number.evolution_instance = instance
        number.connection_state = "open"
        db.add(number)
        db.commit()
        catch_up(number.id)
        return number
    if event == "connection.update":
        return None  # estado da sessão velha não muda o número
    return number


def _just_reset(number: WhatsAppNumber) -> bool:
    """Alguém clicou Desconectar há pouco: o número espera o QR code, não volta pra sessão antiga."""
    return number.reset_at is not None and datetime.datetime.utcnow() - number.reset_at < RESET_KEEP


def _traffic(instance: str) -> int:
    """Quantas mensagens passaram por essa sessão nas últimas 2h (a que recebe de verdade tem movimento)."""
    until = datetime.datetime.utcnow()
    since = until - datetime.timedelta(hours=2)
    try:
        body = evolution_client.find_messages(instance, since.isoformat() + "Z", until.isoformat() + "Z", page=1, per_page=50)
    except evolution_client.EvolutionError:
        return -1
    return int(body.get("total") or len(body.get("records") or []))


def reconcile(db) -> dict:
    """Sessões que sobraram no Evolution de cada número (de um "Desconectar 100%" em que a antiga não
    saiu). Toda rodada:
    - o número usa a sessão que recebe de verdade: se uma antiga funciona e a atual não (ou a antiga tem
      mais movimento nas últimas 2h), o número passa pra ela e puxa as mensagens. Menos logo depois de
      alguém clicar Desconectar: aí ele espera o QR code;
    - sessão antiga conectada continua tendo as mensagens puxadas pro CRM (a cada EXTRA_RETRY) e nunca é
      desconectada pelo CRM sozinho (podia ser a que funciona no celular do vendedor);
    - sessão antiga desconectada: tenta apagar (o Evolution às vezes recusa; não faz mal).
    Só mexe em sessão do mesmo número (mesmo nome de base e, quando dá pra saber, o mesmo telefone)."""
    from app.services import history_sync

    done = {}
    if paused():
        return done
    try:
        sessions = evolution_client.list_instances()
    except evolution_client.EvolutionError:
        return done
    state_of = {name: state for name, state, _ in sessions}
    numbers = db.query(WhatsAppNumber).filter(WhatsAppNumber.provider == "evolution", WhatsAppNumber.is_active.is_(True)).all()
    used = {n.evolution_instance for n in db.query(WhatsAppNumber.evolution_instance)}
    for number in numbers:
        root, phone = instance_root(number.evolution_instance), (number.phone_number or "")[-8:]
        extras = [(name, state) for name, state, owner in sessions
                  if name not in used and instance_root(name) == root and (not phone or not owner or owner[-8:] == phone)]
        if not extras:
            continue
        current = number.evolution_instance
        # logo depois de um Desconectar, enquanto espera o QR, o número não volta pra sessão antiga. Depois
        # que leu o QR (atual conectada), vale a regra de sempre: fica a sessão que recebe de verdade
        # (ex: Guilherme leu o QR e a sessão nova recebia 3 mensagens em 2h; a antiga, 157)
        if not _just_reset(number) or state_of.get(current) == "open":
            candidates = [current] if state_of.get(current) == "open" and _works(current, number.phone_number) else []
            candidates += [name for name, state in extras if state == "open" and _works(name, number.phone_number)]
            best = candidates[0] if candidates else current
            if len(candidates) > 1:
                traffic = {name: _traffic(name) for name in candidates}
                top = max(candidates, key=traffic.get)
                if current not in candidates or traffic[top] > 2 * traffic[current] + 3:  # sem ficar trocando à toa
                    best = top
            if best != current:
                why = "não funciona" if current not in candidates else "quase não recebe mensagem"
                logger.warning("wa_health: %s passou pra sessão %s (a %s %s)", number.label, best, current, why)
                number.evolution_instance, number.connection_state = best, "open"
                db.add(number)
                db.commit()
                used.add(best)
                extras = [(name, state) for name, state in extras if name != best] + [(current, state_of.get(current, ""))]
                current = best
                catch_up(number.id)
                done[number.label] = "passou pra sessão que recebe"
        for name, state in extras:
            now = time.monotonic()
            with _lock:
                tried = _imported.get(name)
                if tried is not None and now - tried < EXTRA_RETRY:
                    continue
                _imported[name] = now
            if state == "open":
                try:  # o webhook dela já entra no número; isto é a garantia
                    history_sync.sync_number(number.id, 0.25, apply=True, instance=name)
                except Exception:
                    logger.exception("wa_health: falhou ao puxar as mensagens da sessão antiga %s", name)
                continue  # 🧘 conectada: o CRM nunca desconecta sozinho (podia ser a que recebe)
            _drop(name)
    return done


def _drop(name: str) -> None:
    for action in (evolution_client.logout, evolution_client.delete_instance):
        try:
            action(name)
        except evolution_client.EvolutionError:
            pass  # o Evolution às vezes recusa (sessão sem conexão): fica lá, sem receber nada


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
            if number is None or paused() or time.monotonic() - _healed.get(number.evolution_instance, -HEAL_COOLDOWN) < 120:
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


def after_night_restart(db, number: WhatsAppNumber) -> str:
    """Depois da renovação da madrugada, pra cada número que funcionava antes: voltou funcionando ->
    puxa as mensagens do intervalo; não voltou nem reiniciando -> avisa o vendedor e os admins, pra
    reconectar (Desconectar + QR) antes de abrir a loja. Devolve "ok", "religou" ou "qr"."""
    from app.services import history_sync

    try:
        works = (evolution_client.connection_state(number.evolution_instance) == "open"
                 and _works(number.evolution_instance, number.phone_number))
    except evolution_client.EvolutionError:
        works = False
    what = "ok" if works else ("religou" if heal(number, "renovação da madrugada") is True else "qr")
    if what == "qr":
        _warn(db, number, _admins_and_owner(db, number), f"📵 {number.label} desconectou de madrugada",
              "Antes de começar o atendimento: " + RECONNECT_TEXT)
        return what
    number.connection_state = "open"
    db.add(number)
    db.commit()
    try:
        history_sync.sync_number(number.id, CATCH_UP_DAYS, apply=True)
    except Exception:
        logger.exception("wa_health: falhou ao puxar o histórico de %s", number.label)
    return what


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


def _silent_after(db, number: WhatsAppNumber, hours: dict, now: datetime.datetime):
    """Quanto tempo sem mensagem já é estranho pra ESTE WhatsApp (None = recebe pouco: não confere)."""
    week = now - datetime.timedelta(days=7)
    count = (db.query(Message.id).join(Conversation, Conversation.id == Message.conversation_id)
             .filter(Conversation.whatsapp_number_id == number.id, Message.direction == "in", Message.created_at >= week)
             .count())
    open_hours = sum((end - start) / 60 for start, end in hours.values()) or 1
    per_hour = count / open_hours
    if per_hour < SILENT_MIN_PER_HOUR:
        return None
    return min(SILENT_MAX, max(SILENT_MIN, datetime.timedelta(minutes=SILENT_GAPS * 60 / per_hour)))


def _last_inbound(db, number: WhatsAppNumber):
    row = (db.query(Message.created_at).join(Conversation, Conversation.id == Message.conversation_id)
           .filter(Conversation.whatsapp_number_id == number.id, Message.direction == "in")
           .order_by(Message.created_at.desc()).first())
    return row[0] if row else None


def check_number(db, number: WhatsAppNumber, now: datetime.datetime) -> str:
    """Confere um número. Devolve o que fez ("", "caiu", "religou", "zumbi", "silencioso")."""
    if paused():
        return ""
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
            # o vigia só reinicia; nunca cria sessão nova sozinho (a antiga podia voltar e ficar recebendo
            # sem o CRM ouvir). Não voltou: o vendedor desconecta e conecta (aí sim 100%, com QR code)
            with _lock:
                warn = ok is False and number.evolution_instance not in _warned
                if warn:
                    _warned[number.evolution_instance] = time.monotonic()
            if warn:  # 1 aviso por problema: só avisa de novo depois que voltar a funcionar e travar outra vez
                logger.warning("wa_health: %s travado: precisa reconectar", number.label)
                _warn(db, number, _admins_and_owner(db, number), f"📵 {number.label} travou",
                      "Diz conectado, mas não envia nem recebe. " + RECONNECT_TEXT)
            return "zumbi"
        return ""
    _warned.pop(number.evolution_instance, None)  # funciona: um travamento novo volta a avisar
    tenant = db.get(Tenant, number.tenant_id)
    hours = parse_hours(tenant.business_hours if tenant else "")
    last = _last_inbound(db, number)
    instance = number.evolution_instance
    if last is not None and now - last < SILENT_MIN:
        _silent_strikes.pop(instance, None)
        _silent_warned.discard(instance)
        return ""
    window = _silent_after(db, number, hours, now) if tenant is not None and last is not None else None
    if window is None or now - last < window:
        if window is not None:
            _silent_strikes.pop(instance, None)
        return ""
    if is_open(tenant, now) and is_open(tenant, now - window) and _others_receiving(db, number, now, window):
        # "parou de receber": diz conectado e passa no teste, mas não chega nada enquanto os outros recebem
        with _lock:
            if time.monotonic() - _silent.get(instance, -window.total_seconds()) < window.total_seconds():
                return ""  # reiniciou há pouco: dá o mesmo tempo pra ver se voltou
            _silent[instance] = time.monotonic()
            strikes = _silent_strikes[instance] = _silent_strikes.get(instance, 0) + 1
        if strikes == 1 and AUTO_RESTART:
            heal(number, "parou de receber mensagens")
            catch_up(number.id)
            return "silencioso"
        catch_up(number.id)  # puxa do Evolution o que tiver escapado
        if instance in _silent_warned:
            return ""  # já avisou: só avisa de novo depois de voltar a receber
        _silent_warned.add(instance)
        # aviso já na 1ª vez (sem reinício automático não há o que esperar), pro vendedor e os admins:
        # a sessão que para de receber em silêncio é o "mensagem que não chega" (ex: Davi das 9h às 13h44)
        _warn(db, number, _admins_and_owner(db, number), f"📵 {number.label} parou de receber mensagens?",
              f"Há {int((now - last).total_seconds() // 60)} min não chega nenhuma mensagem nesse WhatsApp no CRM, "
              "enquanto os outros recebem. Se no celular estão chegando mensagens de clientes: " + RECONNECT_TEXT)
        return "silencioso"
    return ""


def _others_receiving(db, number: WhatsAppNumber, now: datetime.datetime, window: datetime.timedelta) -> bool:
    """Outro WhatsApp do mesmo cliente recebeu mensagem agora há pouco (então não é só um horário parado)."""
    return (db.query(Message.id).join(Conversation, Conversation.id == Message.conversation_id)
            .join(WhatsAppNumber, WhatsAppNumber.id == Conversation.whatsapp_number_id)
            .filter(WhatsAppNumber.tenant_id == number.tenant_id, WhatsAppNumber.id != number.id,
                    Message.direction == "in", Message.created_at >= now - window)
            .first() is not None)


def run() -> dict:
    db = SessionLocal()
    done = {}
    try:
        now = datetime.datetime.utcnow()
        try:
            done.update(reconcile(db))  # sessões que sobraram: nenhuma mensagem fica de fora
        except Exception:
            db.rollback()
            logger.exception("wa_health: falhou ao conferir as sessões que sobraram")
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
