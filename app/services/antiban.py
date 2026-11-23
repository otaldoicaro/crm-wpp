"""🛡️ Anti-bloqueio: as mensagens automáticas não podem sair "em rajada" do mesmo WhatsApp.

Por que existe: um WhatsApp da Nova Viseu tomou restrição de 24h (2026-11) por causa das automáticas.
O WhatsApp desconfia de robô quando o mesmo número manda muitas mensagens iguais, uma colada na outra,
pra quem não está conversando (ex: na abertura da loja saíam de uma vez todos os follow-ups de 24h que
venceram de madrugada; às 8h, todas as 1ªs mensagens dos formulários da noite).

Regras, por WhatsApp (valem pra 1ª mensagem do formulário, follow-up e fora do horário):
- intervalo: entre uma automática e a próxima, um tempo sorteado (GAP); a que não pode sair agora espera
  a próxima passada (a cada minuto) — nada se perde, só é espaçado;
- limite por hora e por dia pras mensagens "frias" (quem não está conversando: 1ª do formulário e follow-up);
- ⏸️ pausa: WhatsApp pausado (botão na tela WhatsApp, ou sozinho depois de FAIL_LIMIT envios automáticos
  seguidos falhando, ou aviso de bloqueio temporário da UAZapi) não manda automática nenhuma até a hora marcada.
  O vendedor continua mandando as dele normalmente.
- {Oi|Olá|Opa}: nos textos das automáticas, cada envio sorteia uma das opções (texto nunca sai idêntico).
"""

from __future__ import annotations

import collections
import datetime
import logging
import random
import re
import threading
import time

logger = logging.getLogger("antiban")

COLD, REPLY = "frio", "resposta"
GAP = {COLD: (90, 180), REPLY: (10, 25)}  # segundos entre automáticas do mesmo WhatsApp
COLD_PER_HOUR = 20
COLD_PER_DAY = 80
FAIL_LIMIT = 3  # automáticas seguidas falhando (sem ser "número sem WhatsApp"): pausa
FAIL_PAUSE_HOURS = 24
BAN_PAUSE_HOURS = 48

_lock = threading.Lock()
_next_ok: dict = {}  # número -> monotonic a partir de quando pode a próxima automática
_cold: dict = collections.defaultdict(collections.deque)  # número -> horários (time.time) das frias
_fails: dict = {}  # número -> falhas seguidas


def paused(number) -> bool:
    until = getattr(number, "auto_paused_until", None)
    return until is not None and until > datetime.datetime.utcnow()


def allow(number, kind: str = COLD) -> bool:
    """Pode sair uma automática deste WhatsApp agora?"""
    if number is None or paused(number):
        return False
    now = time.time()
    with _lock:
        if time.monotonic() < _next_ok.get(number.id, 0):
            return False
        if kind == COLD:
            sent = _cold[number.id]
            while sent and now - sent[0] > 86400:
                sent.popleft()
            if len(sent) >= COLD_PER_DAY or sum(1 for t in sent if now - t < 3600) >= COLD_PER_HOUR:
                return False
    return True


def sent(db, number, kind: str, ok) -> None:
    """Registra o envio (ok: True, None = confirmando, False = falhou) e marca o próximo horário livre."""
    from app.services import messaging

    if number is None:
        return
    low, high = GAP.get(kind, GAP[COLD])
    with _lock:
        _next_ok[number.id] = time.monotonic() + random.uniform(low, high)
        if kind == COLD and ok is not False:
            _cold[number.id].append(time.time())
        if ok is False and not getattr(messaging._last, "no_whatsapp", False):
            _fails[number.id] = _fails.get(number.id, 0) + 1
            trip = _fails[number.id] >= FAIL_LIMIT
        else:
            _fails[number.id], trip = 0, False
    if trip:
        _fails[number.id] = 0
        pause(db, number, FAIL_PAUSE_HOURS, f"{FAIL_LIMIT} mensagens automáticas seguidas não saíram")


def pause(db, number, hours: float, why: str, warn: bool = True) -> None:
    """⏸️ Para as automáticas deste WhatsApp por X horas e avisa o dono (sem dono: admins)."""
    from app.services import wa_health

    number.auto_paused_until = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    db.add(number)
    logger.warning("antiban: automáticas de %s pausadas por %sh (%s)", number.label, hours, why)
    if warn:
        wa_health._warn(db, number, wa_health._admins_and_owner(db, number), f"⏸️ Automáticas pausadas em {number.label}",
                        f"{why[:1].upper()}{why[1:]}. Pra proteger o número de bloqueio, o CRM parou as mensagens automáticas "
                        f"dele por {int(hours)}h. As mensagens que você manda continuam normais.")
    db.commit()


def resume(db, number) -> None:
    number.auto_paused_until = None
    db.add(number)
    db.commit()


_SPIN = re.compile(r"\{([^{}|]*\|[^{}]*)\}")


def vary(text: str) -> str:
    """{Oi|Olá|Opa} -> uma das opções, sorteada a cada envio. {nome} e afins (sem "|") ficam como estão."""
    for _ in range(10):
        new = _SPIN.sub(lambda m: random.choice(m.group(1).split("|")), text)
        if new == text:
            break
        text = new
    return text
