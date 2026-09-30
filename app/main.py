import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import sync_schema
from app.services import media_store
from app.routers import (
    admin_setup,
    auth_router,
    bridge,
    dashboard,
    webhooks_evolution,
    webhooks_site,
    webhooks_whatsapp,
    whatsapp_connect,
)
from app.tenancy import DEBUG

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="CRM Multi-Tenant WhatsApp + Site")

sync_schema()
media_store.setup()


@app.middleware("http")
async def debug_tenant_cookie_middleware(request, call_next):
    """Em DEBUG=1 (sem subdomínio real disponível ainda, ex: onrender.com),
    grava o '?tenant=' usado nesta requisição num cookie, pra sobreviver a
    redirects internos (login, troca de etapa) que não repassam esse query
    param. Precisa ser middleware (não dependency) porque rotas que devolvem
    um RedirectResponse próprio ignoram cookies setados via dependency."""
    response = await call_next(request)
    if DEBUG:
        tenant_param = request.query_params.get("tenant")
        if tenant_param:
            response.set_cookie("debug_tenant", tenant_param, httponly=True, samesite="lax")
    return response

# serve app/static em /static — é aqui que fica o whatsapp-bridge.js pro site do cliente
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(admin_setup.router)
app.include_router(webhooks_whatsapp.router)
app.include_router(webhooks_evolution.router)
app.include_router(webhooks_site.router)
app.include_router(bridge.router)
app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(whatsapp_connect.router)


@app.get("/health")
def health():
    return {"ok": True}
