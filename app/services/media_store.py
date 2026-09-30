"""Cópia própria das mídias (foto, áudio, vídeo, documento) das conversas.

Sem isso, o CRM busca a mídia no WhatsApp na hora de abrir a conversa — e se
o número for bloqueado/desconectado, ou o WhatsApp apagar o arquivo dos
servidores dele (acontece depois de algumas semanas), a mídia some.

Guarda num armazenamento compatível com S3 (SeaweedFS no VPS da Hostinger,
ou Cloudflare R2/AWS S3), configurado por MEDIA_S3_*. Sem configuração, fica
desligado e o CRM continua buscando direto no WhatsApp como antes.

Limpeza automática: cada arquivo é apagado MEDIA_RETENTION_DAYS dias depois
de salvo. Tentamos uma regra de expiração no próprio bucket e, como nem todo
armazenamento suporta, o CRM também faz a limpeza ele mesmo 1x por dia
(`cleanup_expired`). Só a mídia é apagada; texto das conversas e dados do
lead ficam pra sempre no banco.
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Optional

from app.config import (
    MEDIA_RETENTION_DAYS,
    MEDIA_S3_ACCESS_KEY,
    MEDIA_S3_BUCKET,
    MEDIA_S3_ENDPOINT,
    MEDIA_S3_SECRET_KEY,
)

logger = logging.getLogger("media_store")
_client = None


def is_enabled() -> bool:
    return bool(MEDIA_S3_ENDPOINT and MEDIA_S3_ACCESS_KEY and MEDIA_S3_SECRET_KEY)


def _s3():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=MEDIA_S3_ENDPOINT,
            aws_access_key_id=MEDIA_S3_ACCESS_KEY,
            aws_secret_access_key=MEDIA_S3_SECRET_KEY,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
        )
    return _client


def setup() -> None:
    """Cria o bucket (se faltar) e aplica a regra de expiração. Roda no start."""
    if not is_enabled():
        logger.info("media_store desligado (MEDIA_S3_* não configurado)")
        return
    try:
        s3 = _s3()
        existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
        if MEDIA_S3_BUCKET not in existing:
            s3.create_bucket(Bucket=MEDIA_S3_BUCKET)
    except Exception:  # não pode derrubar o CRM se o armazenamento estiver fora do ar
        logger.exception("media_store: falha ao preparar o bucket")
        return
    try:
        s3.put_bucket_lifecycle_configuration(
            Bucket=MEDIA_S3_BUCKET,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": "apaga-midia-antiga",
                        "Status": "Enabled",
                        "Filter": {"Prefix": ""},
                        "Expiration": {"Days": MEDIA_RETENTION_DAYS},
                    }
                ]
            },
        )
    except Exception:
        logger.info("media_store: armazenamento sem regra de expiração; limpeza fica por conta do CRM")
    logger.info("media_store ok: bucket %s, retenção %s dias", MEDIA_S3_BUCKET, MEDIA_RETENTION_DAYS)
    _schedule_cleanup(delay_seconds=60)


def cleanup_expired() -> int:
    """Apaga arquivos com mais de MEDIA_RETENTION_DAYS dias. Devolve quantos apagou."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=MEDIA_RETENTION_DAYS)
    s3 = _s3()
    deleted = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=MEDIA_S3_BUCKET):
        old = [{"Key": o["Key"]} for o in page.get("Contents", []) if o["LastModified"] < cutoff]
        for i in range(0, len(old), 1000):
            s3.delete_objects(Bucket=MEDIA_S3_BUCKET, Delete={"Objects": old[i : i + 1000], "Quiet": True})
            deleted += len(old[i : i + 1000])
    return deleted


def _schedule_cleanup(delay_seconds: int) -> None:
    def run():
        try:
            n = cleanup_expired()
            if n:
                logger.info("media_store: %s mídias com mais de %s dias apagadas", n, MEDIA_RETENTION_DAYS)
        except Exception:
            logger.exception("media_store: falha na limpeza")
        _schedule_cleanup(delay_seconds=24 * 60 * 60)

    timer = threading.Timer(delay_seconds, run)
    timer.daemon = True
    timer.start()


def save(message_id: str, content: bytes, mime_type: str) -> str:
    """Salva e devolve a chave do arquivo, ou '' se falhar/estiver desligado."""
    if not is_enabled() or not content:
        return ""
    key = f"{message_id}"
    try:
        _s3().put_object(Bucket=MEDIA_S3_BUCKET, Key=key, Body=content, ContentType=mime_type or "application/octet-stream")
        return key
    except Exception:
        logger.exception("media_store: falha ao salvar %s", key)
        return ""


def load(key: str) -> tuple[Optional[bytes], Optional[str]]:
    if not is_enabled() or not key:
        return None, None
    try:
        obj = _s3().get_object(Bucket=MEDIA_S3_BUCKET, Key=key)
        return obj["Body"].read(), obj.get("ContentType") or "application/octet-stream"
    except Exception:
        return None, None  # expirou (retenção) ou armazenamento fora do ar


def backup_message_media(message_id: str) -> None:
    """Baixa a mídia de uma mensagem recebida no WhatsApp e guarda a cópia.
    Roda em segundo plano depois do webhook responder (download pode demorar)."""
    if not is_enabled():
        return
    from app.db import SessionLocal
    from app.models import Conversation, Message, WhatsAppNumber
    from app.services import messaging

    db = SessionLocal()
    try:
        message = db.get(Message, message_id)
        if not message or not message.media_id or message.media_stored_key:
            return
        conversation = db.get(Conversation, message.conversation_id)
        number = db.get(WhatsAppNumber, conversation.whatsapp_number_id)
        content, mime_type = messaging.fetch_media(number, message.media_id)
        key = save(message.id, content, mime_type) if content else ""
        if key:
            message.media_stored_key = key
            db.add(message)
            db.commit()
    except Exception:
        logger.exception("media_store: falha no backup da mensagem %s", message_id)
    finally:
        db.close()
