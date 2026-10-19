"""Onde o WhatsApp conectado (Baileys, dentro do Evolution) guarda a tabela "código LID ->
telefone": no Redis do Evolution, um hash por WhatsApp (<prefixo>:instance:<id>), campo
"lid-mapping-<lid>_reverse" com o telefone. Aqui o CRM só LÊ (nunca escreve no Redis).

Sem EVOLUTION_REDIS_URL (ou com o Redis fora do ar) tudo devolve vazio e o CRM segue como antes."""

from __future__ import annotations

import json
import logging
import time

from app.config import EVOLUTION_REDIS_URL

logger = logging.getLogger("lid_store")

_client = None
_hashes: list = []
_hashes_at = 0.0
HASHES_TTL = 600  # relista os WhatsApps conectados a cada 10 min


def _redis():
    global _client
    if not EVOLUTION_REDIS_URL:
        return None
    if _client is None:
        import redis

        _client = redis.Redis.from_url(EVOLUTION_REDIS_URL, socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
    return _client


def _instance_hashes(r) -> list:
    global _hashes, _hashes_at
    if not _hashes or time.monotonic() - _hashes_at > HASHES_TTL:
        _hashes = [k for k in r.scan_iter(match="*:instance:*", count=500) if r.type(k) == "hash"]
        _hashes_at = time.monotonic()
    return _hashes


def _digits(value) -> str:
    try:
        value = json.loads(value)
    except (TypeError, ValueError):
        pass
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if 8 <= len(digits) <= 15 else ""


def phone_for(lid: str) -> str:
    """Telefone de um LID, consultando na hora. Vazio se não souber (ou sem Redis)."""
    r = _redis()
    if r is None or not lid.isdigit():
        return ""
    try:
        for key in _instance_hashes(r):
            phone = _digits(r.hget(key, f"lid-mapping-{lid}_reverse"))
            if phone:
                return phone
    except Exception as exc:  # Redis fora do ar: não trava a mensagem
        logger.warning("lid_store: não consegui ler o Redis do Evolution: %s", exc)
    return ""


def all_mappings() -> dict:
    """{lid: telefone} de todos os WhatsApps conectados (pra importação de 15 em 15 min)."""
    r = _redis()
    if r is None:
        return {}
    found = {}
    try:
        for key in _instance_hashes(r):
            for field, value in r.hscan_iter(key, match="lid-mapping-*_reverse", count=1000):
                lid = field[len("lid-mapping-"):-len("_reverse")]
                phone = _digits(value)
                if lid.isdigit() and phone:
                    found[lid] = phone
    except Exception as exc:
        logger.warning("lid_store: não consegui ler o Redis do Evolution: %s", exc)
    return found
