"""📊 Recebe o gasto do Google Ads mandado pelo script da conta do cliente (services/google_ads_spend.py)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.db import SessionLocal
from app.models import Tenant
from app.services import google_ads_spend

router = APIRouter()


def _save(tenant_id: str, payload: dict):
    db = SessionLocal()
    try:
        if db.get(Tenant, tenant_id) is None:
            return None
        return google_ads_spend.save(db, tenant_id, payload)
    finally:
        db.close()


@router.post("/webhooks/google-ads/{tenant_id}")
async def receive_spend(tenant_id: str, request: Request):
    if not google_ads_spend.valid_key(tenant_id, request.query_params.get("chave", "")):
        return JSONResponse({"ok": False, "error": "chave inválida: copie o script de novo em Configurações → Integrações"}, status_code=403)
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "formato inesperado"}, status_code=400)
    saved = await run_in_threadpool(_save, tenant_id, payload)
    if saved is None:
        return JSONResponse({"ok": False, "error": "cliente não encontrado"}, status_code=404)
    return JSONResponse({"ok": True, "linhas": saved})
