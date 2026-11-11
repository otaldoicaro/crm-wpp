import logging

import html

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles

from app.db import SessionLocal, setup_text_search, sync_indexes, sync_schema
from app.services import archiver, evolution_client, followup, funnel, history_sync, media_store, meta_spend, people, response_times
from app.routers import (
    admin_setup,
    audit,
    auth_router,
    bridge,
    profile,
    quick_replies,
    team,
    dashboard,
    webhooks_evolution,
    webhooks_uazapi,
    webhooks_site,
    webhooks_whatsapp,
    whatsapp_connect,
)
from app.routers import google_ads as google_ads_router
from app.routers import instagram as instagram_router
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
    funnel.won_are_customers(_fdb)  # quem já comprou (Ganho) vira Cliente
archiver.schedule()
meta_spend.schedule()
history_sync.schedule()
from app.services import wa_health  # noqa: E402

wa_health.schedule()  # 🩺 confere os WhatsApps a cada 5 min e religa sozinho o que travar
evolution_client.refresh_webhooks()  # eventos novos do webhook (📞 ligações) valem sem reconectar
from app.services.conversions import queue as conversions_queue  # noqa: E402

conversions_queue.schedule()
from app.services import instagram as _instagram  # noqa: E402

_instagram.schedule()  # 📸 renova o token das contas do Instagram conectadas  # 📤 eventos do CRM pro Meta (por cliente)  # recupera do Evolution mensagens que escaparam (a cada 5 min, última 1h)  # gasto dos anúncios do Meta (a cada 3h; só com META_ADS_ACCESS_TOKEN)
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
app.include_router(webhooks_uazapi.router)
app.include_router(webhooks_site.router)
app.include_router(instagram_router.router)
app.include_router(google_ads_router.router)
app.include_router(bridge.router)
app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(whatsapp_connect.router)
app.include_router(team.router)
app.include_router(audit.router)
app.include_router(quick_replies.router)
app.include_router(profile.router)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/privacidade", response_class=HTMLResponse)
def privacy_policy(request: Request):
    """Política de privacidade pública (a Meta pede o endereço pra publicar o app do Instagram/WhatsApp).
    Usa o nome do cliente do domínio; sem cliente, o nome genérico."""
    from app.tenancy import get_tenant

    db = SessionLocal()
    try:
        tenant = get_tenant(request, db)
        name = html.escape(tenant.name if tenant else "nossa empresa")
    finally:
        db.close()
    body = f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Política de privacidade · {name}</title>
<div style="font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.6;color:#222">
<h1>Política de privacidade</h1>
<p>Esta política explica como {name} trata os dados de quem fala com a gente pelo WhatsApp, pelo Direct do
Instagram, pelos formulários e pelo chat do nosso site.</p>
<h2>Quais dados recebemos</h2>
<p>Nome, telefone, e-mail, nome de usuário do Instagram e as mensagens e arquivos que você nos envia, além das
respostas que você preenche nos nossos formulários e de onde você chegou até nós (por exemplo, um anúncio).</p>
<h2>Para que usamos</h2>
<p>Só para responder e atender você: tirar dúvidas, enviar propostas, combinar reuniões e acompanhar o seu
atendimento. Usamos também, de forma agregada, para medir quais canais e campanhas trazem contatos.</p>
<h2>Com quem compartilhamos</h2>
<p>Não vendemos seus dados. Eles ficam no nosso sistema de atendimento (CRM) e só a nossa equipe tem acesso.
Plataformas que usamos para conversar com você (Meta: WhatsApp e Instagram; Google) seguem as políticas delas.</p>
<h2>Por quanto tempo guardamos</h2>
<p>Enquanto houver relação de atendimento ou comercial, ou pelo prazo exigido por lei.</p>
<h2>Seus direitos (LGPD)</h2>
<p>Você pode pedir a qualquer momento para ver, corrigir ou apagar os seus dados. Basta mandar uma mensagem
pelo mesmo canal em que falou com a gente (WhatsApp ou Direct do Instagram) pedindo a exclusão, e removemos
o seu cadastro e as suas conversas.</p>
<p style="color:#777;font-size:.9em">Última atualização: novembro de 2026.</p>
</div></html>"""
    return HTMLResponse(body)


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
