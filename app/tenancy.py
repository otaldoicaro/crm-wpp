from __future__ import annotations

import os

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import BASE_DOMAIN, PUBLIC_BASE_URL
from app.models import Tenant

DEBUG = os.getenv("DEBUG", "1") == "1"


def resolve_subdomain(request: Request) -> str | None:
    """Extrai o subdomínio do Host. Ex: 'clientea.suacrm.com.br' -> 'clientea'.

    Em dev/teste (sem subdomínio de verdade disponível ainda), também aceita
    '?tenant=clientea' na URL (só quando DEBUG=1). Como redirects internos
    (login, troca de etapa, etc.) não carregam esse query param, o valor é
    também guardado num cookie 'debug_tenant' (ver deps.py::current_tenant)
    pra sobreviver aos redirects dentro da mesma sessão do navegador.
    Também aceita hosts como 'clientea.localhost:8000' (o Chrome/macOS já
    resolvem *.localhost para 127.0.0.1 sem precisar mexer no /etc/hosts).
    """
    if DEBUG:
        override = request.query_params.get("tenant")
        if override:
            return override
        cookie_override = request.cookies.get("debug_tenant")
        if cookie_override:
            return cookie_override

    host = request.headers.get("host", "")
    host = host.split(":")[0]  # remove porta

    if host == BASE_DOMAIN or host == "localhost" or host == "127.0.0.1":
        return None

    if host.endswith("." + BASE_DOMAIN):
        return host[: -(len(BASE_DOMAIN) + 1)]

    if host.endswith(".localhost"):
        return host[: -len(".localhost")]

    return None


def request_host(request: Request) -> str:
    return request.headers.get("host", "").split(":")[0].lower()


def get_tenant(request: Request, db: Session) -> Tenant | None:
    # domínio próprio do cliente (ex: crm.novaviseu.com.br) tem prioridade
    host = request_host(request)
    if host:
        tenant = db.query(Tenant).filter(Tenant.custom_domain == host).first()
        if tenant:
            return tenant
    subdomain = resolve_subdomain(request)
    if not subdomain:
        return None
    return db.query(Tenant).filter(Tenant.subdomain == subdomain).first()


def is_known_host(db: Session, host: str) -> bool:
    """Usado pelo Caddy antes de emitir certificado HTTPS pra um domínio:
    só emite pra domínio de cliente cadastrado (evita abuso)."""
    host = (host or "").lower()
    if not host:
        return False
    if db.query(Tenant.id).filter(Tenant.custom_domain == host).first():
        return True
    if BASE_DOMAIN and host.endswith("." + BASE_DOMAIN):
        sub = host[: -(len(BASE_DOMAIN) + 1)]
        return db.query(Tenant.id).filter(Tenant.subdomain == sub).first() is not None
    return False


def public_url(tenant) -> str:
    """Endereço do CRM DESTE cliente pra links que vão pra fora (convite, link rotativo...):
    o domínio próprio (https://crm.cliente.com.br); sem ele, o endereço reserva
    https://<identificador>.<BASE_DOMAIN>. Nunca o PUBLIC_BASE_URL, que é um só pro servidor
    inteiro (e mostraria o domínio de outro cliente)."""
    if tenant is not None and tenant.custom_domain:
        return f"https://{tenant.custom_domain}"
    if tenant is not None and BASE_DOMAIN and BASE_DOMAIN != "localhost":
        return f"https://{tenant.subdomain}.{BASE_DOMAIN}"
    return PUBLIC_BASE_URL
