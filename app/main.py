import logging

import html

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles

from app.db import SessionLocal, setup_text_search, sync_indexes, sync_schema
from app.services import archiver, followup, funnel, history_sync, media_store, meta_spend, people, response_times
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
meta_spend.schedule()
history_sync.schedule()  # recupera do Evolution mensagens que escaparam (a cada 1h, último dia)  # gasto dos anúncios do Meta (a cada 3h; só com META_ADS_ACCESS_TOKEN)
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

READ_ONLY_OK = {"/logout", "/login"}  # o que um visitante "só visualização" pode enviar
READ_ONLY_HIDDEN = ("/equipe", "/whatsapp", "/lixeira", "/admin")  # nem abre (convites, QR code, e-mails)
READ_ONLY_MSG = "Acesso só de visualização: nada pode ser alterado."


def _read_only_user(token: str) -> bool:
    from app.auth import read_session_token
    from app.models import User

    data = read_session_token(token) if token else None
    if not data:
        return False
    with SessionLocal() as db:
        user = db.get(User, data.get("user_id"))
        return bool(user and user.read_only)


@app.middleware("http")
async def read_only_guard(request: Request, call_next):
    """Visitante "só visualização": qualquer envio (salvar, mandar mensagem, mover lead...) é
    recusado aqui, antes de chegar em qualquer tela. Conferido no servidor, não só escondendo botão."""
    path = request.url.path
    writes = request.method not in ("GET", "HEAD", "OPTIONS") and path not in READ_ONLY_OK
    hidden = request.method == "GET" and path.startswith(READ_ONLY_HIDDEN)
    if (writes or hidden) and not path.startswith(("/webhooks/", "/static/")):
        from fastapi.concurrency import run_in_threadpool
        from app.config import SESSION_COOKIE_NAME

        if await run_in_threadpool(_read_only_user, request.cookies.get(SESSION_COOKIE_NAME, "")):
            if hidden:
                return RedirectResponse(url="/dashboard", status_code=302)
            if "text/html" in request.headers.get("accept", ""):
                back = request.headers.get("referer") or "/"
                return RedirectResponse(url=back.split("?")[0] + "?erro=" + READ_ONLY_MSG.replace(" ", "+"), status_code=303)
            return JSONResponse({"ok": False, "error": READ_ONLY_MSG, "detail": READ_ONLY_MSG}, status_code=403)
    return await call_next(request)


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
