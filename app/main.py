import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.routers import admin_setup, auth_router, bridge, dashboard, webhooks_site, webhooks_whatsapp

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="CRM Multi-Tenant WhatsApp + Site")

Base.metadata.create_all(bind=engine)

# serve app/static em /static — é aqui que fica o whatsapp-bridge.js pro site do cliente
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(admin_setup.router)
app.include_router(webhooks_whatsapp.router)
app.include_router(webhooks_site.router)
app.include_router(bridge.router)
app.include_router(auth_router.router)
app.include_router(dashboard.router)


@app.get("/health")
def health():
    return {"ok": True}
