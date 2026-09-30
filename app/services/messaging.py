"""Envio/recebimento de mídia independente do canal: o Inbox chama estas
funções e elas decidem se falam com a Cloud API oficial ou com o Evolution,
conforme o `provider` do número da conversa."""

from __future__ import annotations

from typing import Optional

from app.models import WhatsAppNumber
from app.services import evolution_client, whatsapp_client
from app.services.whatsapp_client import media_kind_for_mime  # noqa: F401  (reexportado pro Inbox)


def send_text(number: WhatsAppNumber, phone: str, body: str) -> tuple[bool, str]:
    """Devolve (ok, id_da_mensagem_no_whatsapp)."""
    if number.provider == "evolution":
        try:
            return True, evolution_client.send_text(number.evolution_instance, phone, body)
        except evolution_client.EvolutionError:
            return False, ""
    result = whatsapp_client.send_text_message(number, phone, body)
    wa_id = result.get("body", {}).get("messages", [{}])[0].get("id", "") if result["ok"] else ""
    return result["ok"], wa_id


def send_media(
    number: WhatsAppNumber, phone: str, content: bytes, filename: str, mime_type: str, media_kind: str, caption: str
) -> tuple[bool, str, str, str]:
    """Devolve (ok, id_da_mensagem, media_id_pra_guardar, erro)."""
    if number.provider == "evolution":
        try:
            wa_id = evolution_client.send_media(
                number.evolution_instance, phone, content, filename, mime_type, media_kind, caption
            )
        except evolution_client.EvolutionError as exc:
            return False, "", "", str(exc)
        # no Evolution a mídia é buscada depois pelo id da própria mensagem
        return True, wa_id, wa_id, ""

    media_id = whatsapp_client.upload_media(number, content, filename, mime_type)
    if not media_id:
        return False, "", "", "falha ao enviar arquivo pra Meta"
    result = whatsapp_client.send_media_message(number, phone, media_id, media_kind, caption)
    wa_id = result.get("body", {}).get("messages", [{}])[0].get("id", "") if result["ok"] else ""
    return result["ok"], wa_id, media_id, "" if result["ok"] else "Meta recusou o envio"


def fetch_media(number: WhatsAppNumber, media_id: str) -> tuple[Optional[bytes], Optional[str]]:
    if number.provider == "evolution":
        return evolution_client.fetch_media(number.evolution_instance, media_id)
    return whatsapp_client.fetch_media(number, media_id)
