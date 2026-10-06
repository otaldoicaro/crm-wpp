"""Lê mensagens editadas que chegam criptografadas (secretEncryptedMessage).

Quando alguém edita uma mensagem, o WhatsApp manda o texto novo cifrado com uma
chave derivada do "messageSecret" da mensagem ORIGINAL (que guardamos em
Message.secret quando ela chegou). Mesmo método do whatsmeow (msgsecret.go):

    chave = HKDF-SHA256(messageSecret, salt=vazio,
                        info = id_original + jid_autor_original + jid_quem_editou + "Message Edit")
    texto = AES-256-GCM(chave, iv=encIv).decrypt(encPayload)   # sem dados adicionais

O resultado é um protobuf "Message"; aqui só extraímos o texto.
"""

from __future__ import annotations

import base64
import itertools
from typing import Iterable, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MESSAGE_EDIT = "Message Edit"


def to_bytes(value) -> bytes:
    """O Evolution manda binário como base64, lista de números, {"0": 12, "1": 34...}
    ou {"type": "Buffer", "data": [...]} dependendo do caminho. Aceita todos."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return base64.b64decode(value)
        except ValueError:
            return b""
    if isinstance(value, list):
        return bytes(value)
    if isinstance(value, dict):
        if "data" in value and isinstance(value["data"], list):
            return bytes(value["data"])
        if value and all(k.isdigit() for k in value):
            return bytes(value[k] for k in sorted(value, key=int))
    return b""


def secret_b64(message: dict) -> str:
    """messageSecret da mensagem (pra guardar), em base64; '' se não veio."""
    raw = to_bytes(((message or {}).get("messageContextInfo") or {}).get("messageSecret"))
    return base64.b64encode(raw).decode() if raw else ""


def non_ad(jid: str) -> str:
    """'5521999:12@s.whatsapp.net' -> '5521999@s.whatsapp.net' (tira o número do aparelho)."""
    if not jid or "@" not in jid:
        return ""
    user, server = jid.split("@", 1)
    return f"{user.split(':')[0]}@{server}"


def _key(orig_secret: bytes, orig_id: str, orig_sender: str, mod_sender: str, use_case: str) -> bytes:
    info = (orig_id + orig_sender + mod_sender + use_case).encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(orig_secret)


def decrypt_edit(
    orig_secret_b64: str, orig_id: str, sender_candidates: Iterable[str], enc_payload: bytes, enc_iv: bytes
) -> Optional[bytes]:
    """Tenta abrir a edição com cada JID possível do autor (número ou LID — o WhatsApp
    está migrando pra LID e o mesmo contato pode aparecer dos dois jeitos)."""
    secret = base64.b64decode(orig_secret_b64) if orig_secret_b64 else b""
    jids = [j for j in dict.fromkeys(non_ad(j) for j in sender_candidates) if j]
    if not secret or not jids or not enc_payload or not enc_iv:
        return None
    for orig_sender, mod_sender in itertools.product(jids, repeat=2):
        try:
            return AESGCM(_key(secret, orig_id, orig_sender, mod_sender, MESSAGE_EDIT)).decrypt(enc_iv, enc_payload, None)
        except Exception:
            continue
    return None


# ---------- leitura mínima de protobuf (só o que precisamos: o texto) ----------

def _fields(data: bytes):
    i, n = 0, len(data)
    while i < n:
        tag, i = _varint(data, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            _, i = _varint(data, i)
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        elif wire == 2:
            size, i = _varint(data, i)
            yield field, data[i : i + size]
            i += size
        else:
            return


def _varint(data: bytes, i: int):
    shift = result = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def text_from_message(data: bytes, depth: int = 0) -> str:
    """Texto de um protobuf Message: conversation (1), extendedTextMessage.text (6.1),
    legenda de imagem (3.3) / vídeo (9.7), ou o editedMessage (12.14) de um protocolMessage."""
    if depth > 4:
        return ""
    try:
        for field, value in _fields(data):
            if field == 1:
                return value.decode("utf-8", "replace")
            if field in (6, 3, 9):
                wanted = {6: 1, 3: 3, 9: 7}[field]
                for sub_field, sub_value in _fields(value):
                    if sub_field == wanted:
                        return sub_value.decode("utf-8", "replace")
            if field == 12:
                for sub_field, sub_value in _fields(value):
                    if sub_field == 14:
                        return text_from_message(sub_value, depth + 1)
    except (IndexError, ValueError):
        return ""
    return ""
