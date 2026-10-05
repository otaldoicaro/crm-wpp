"""Envio de e-mail por SMTP (hoje só o link de "Esqueceu a senha?").
Configurado por SMTP_* (deploy/vps/configurar_email.sh preenche no VPS)."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.config import SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USER

logger = logging.getLogger("mailer")


def is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def send_email(to: str, subject: str, text: str, html: str = "", from_name: str = "CRM") -> bool:
    if not is_configured():
        logger.warning("e-mail não enviado (SMTP não configurado): %s", subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, SMTP_FROM))
    msg["To"] = to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=20) as smtp:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.starttls(context=context)
                smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(msg)
        return True
    except Exception:
        logger.exception("falha ao enviar e-mail pra %s", to)
        return False
