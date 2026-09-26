from __future__ import annotations

import os

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import BASE_DOMAIN
from app.models import Tenant

DEBUG = os.getenv("DEBUG", "1") == "1"


def resolve_subdomain(request: Request) -> str | None:
    """Extrai o subdomínio do Host. Ex: 'clientea.suacrm.com.br' -> 'clientea'.

    Em dev, também aceita '?tenant=clientea' na URL (só quando DEBUG=1) e
    hosts como 'clientea.localhost:8000' (o Chrome/macOS já resolvem
    *.localhost para 127.0.0.1 sem precisar mexer no /etc/hosts).
    """
    if DEBUG:
        override = request.query_params.get("tenant")
        if override:
            return override

    host = request.headers.get("host", "")
    host = host.split(":")[0]  # remove porta

    if host == BASE_DOMAIN or host == "localhost" or host == "127.0.0.1":
        return None

    if host.endswith("." + BASE_DOMAIN):
        return host[: -(len(BASE_DOMAIN) + 1)]

    if host.endswith(".localhost"):
        return host[: -len(".localhost")]

    return None


def get_tenant(request: Request, db: Session) -> Tenant | None:
    subdomain = resolve_subdomain(request)
    if not subdomain:
        return None
    return db.query(Tenant).filter(Tenant.subdomain == subdomain).first()
