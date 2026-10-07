import logging

import html

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles

from app.db import SessionLocal, setup_text_search, sync_indexes, sync_schema
from app.services import archiver, followup, funnel, media_store, meta_spend, people, response_times
from app.routers import (
    admin_setup,
    auth_router,
    bridge,
    quick_replies,
    team,
    dashboard,
    webhooks_evolution,
    webhooks_site,
    webhooks_whatsapp,
    whatsapp_connect,
)
from app.tenancy import DEBUG, is_known_host

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="CRM Multi-Tenant WhatsApp + Site")

sync_schema()
sync_indexes()
setup_text_search()  # índice pra busca dentro das conversas
media_store.setup()
with SessionLocal() as _fdb:
    funnel.setup(_fdb)  # etapa "Negociando" + etapa mais avançada dos leads antigos
    funnel.move_answered_to_service(_fdb)
    followup.setup(_fdb)  # prazos por etapa (padrão) + dados de follow-up dos leads antigos  # quem o time já respondeu sai de "Novo"
    people.admins_out_of_rotation(_fdb)  # admin só no rodízio se alguém ligar e confirmar
archiver.schedule()
meta_spend.schedule()  # gasto dos anúncios do Meta (a cada 3h; só com META_ADS_ACCESS_TOKEN)
webhooks_evolution.clean_legacy_placeholders()
with SessionLocal() as _db:
    response_times.backfill(_db)


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

@app.exception_handler(StarletteHTTPException)
async def friendly_http_errors(request: Request, exc: StarletteHTTPException):
    """Quem abre uma página do painel pelo navegador (ex: link no WhatsApp)
    sem estar logado vai pra tela de login, em vez de ver um JSON técnico."""
    wants_html = request.method == "GET" and "text/html" in request.headers.get("accept", "")
    if wants_html and exc.status_code == 401:
        return RedirectResponse(url="/login", status_code=302)
    if wants_html and exc.status_code in (403, 404):
        message = html.escape(str(exc.detail or "Página não encontrada"))
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<div style='font-family:sans-serif;max-width:420px;margin:15vh auto;padding:0 20px;text-align:center'>"
            f"<p style='font-size:1.1rem'>{message}</p><p><a href='/'>Voltar pro início</a></p></div>",
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, media_type="application/json; charset=utf-8")


app.include_router(admin_setup.router)
app.include_router(webhooks_whatsapp.router)
app.include_router(webhooks_evolution.router)
app.include_router(webhooks_site.router)
app.include_router(bridge.router)
app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(whatsapp_connect.router)
app.include_router(team.router)
app.include_router(quick_replies.router)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/internal/caddy-ask")
def caddy_ask(domain: str = ""):
    """O Caddy (HTTPS automático no VPS) pergunta aqui se pode emitir
    certificado pra `domain`. 200 = é domínio de cliente nosso."""
    db = SessionLocal()
    try:
        ok = is_known_host(db, domain)
    finally:
        db.close()
    return Response(status_code=200 if ok else 404)
