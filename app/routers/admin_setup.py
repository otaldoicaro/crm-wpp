"""Endpoint de provisionamento inicial, usado só durante a configuração (por
mim, via curl) pra criar um tenant + número de WhatsApp num ambiente recém
publicado, sem precisar de acesso a shell/console do provedor de hospedagem.

Protegido por um header secreto (X-Setup-Token, igual ao ADMIN_SETUP_TOKEN do
.env). Se ADMIN_SETUP_TOKEN não estiver configurado no ambiente, o endpoint
fica sempre desativado (nunca aceita chamada nenhuma) — assim não corre risco
de ficar uma porta aberta esquecida em produção.
"""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import ADMIN_SETUP_TOKEN
from app.db import SessionLocal
from app.models import PipelineStage, Tenant, User, WhatsAppNumber

router = APIRouter()


class WhatsAppNumberIn(BaseModel):
    label: str = "Número principal"
    phone_number: str
    waba_phone_number_id: str
    waba_business_account_id: str = ""
    access_token: str


class BootstrapIn(BaseModel):
    subdomain: str
    tenant_name: str
    admin_email: str
    admin_password: str
    whatsapp: Optional[WhatsAppNumberIn] = None


def _require_setup_enabled(x_setup_token: Optional[str]):
    if not ADMIN_SETUP_TOKEN or x_setup_token != ADMIN_SETUP_TOKEN:
        raise HTTPException(status_code=404, detail="not found")


@router.post("/admin/bootstrap-tenant")
def bootstrap_tenant(payload: BootstrapIn, x_setup_token: Optional[str] = Header(default=None)):
    _require_setup_enabled(x_setup_token)

    db: Session = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.subdomain == payload.subdomain).first()
        created_tenant = False
        if not tenant:
            tenant = Tenant(name=payload.tenant_name, subdomain=payload.subdomain)
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
            created_tenant = True

            default_stages = [
                ("Novo", 0, "", False, False),
                ("Em atendimento", 1, "", False, False),
                ("Qualificado", 2, "Qualified", False, False),
                ("Ganho", 3, "Purchase", True, False),
                ("Perdido", 4, "", False, True),
            ]
            for name, order, event_name, is_won, is_lost in default_stages:
                db.add(
                    PipelineStage(
                        tenant_id=tenant.id,
                        name=name,
                        order=order,
                        conversion_event_name=event_name,
                        is_won=is_won,
                        is_lost=is_lost,
                    )
                )
            db.commit()

        admin = (
            db.query(User)
            .filter(User.tenant_id == tenant.id, User.email == payload.admin_email.lower().strip())
            .first()
        )
        if not admin:
            admin = User(
                tenant_id=tenant.id,
                name="Admin",
                email=payload.admin_email.lower().strip(),
                password_hash=hash_password(payload.admin_password),
                role="admin",
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
        else:
            admin.password_hash = hash_password(payload.admin_password)
            db.add(admin)
            db.commit()

        whatsapp_number_id = None
        if payload.whatsapp:
            number = (
                db.query(WhatsAppNumber)
                .filter(WhatsAppNumber.waba_phone_number_id == payload.whatsapp.waba_phone_number_id)
                .first()
            )
            if not number:
                number = WhatsAppNumber(tenant_id=tenant.id, waba_phone_number_id=payload.whatsapp.waba_phone_number_id)
            number.tenant_id = tenant.id
            number.label = payload.whatsapp.label
            number.phone_number = payload.whatsapp.phone_number
            number.waba_business_account_id = payload.whatsapp.waba_business_account_id
            number.access_token = payload.whatsapp.access_token
            number.is_active = True
            db.add(number)
            db.commit()
            db.refresh(number)
            whatsapp_number_id = number.id

        return {
            "ok": True,
            "created_tenant": created_tenant,
            "tenant_id": tenant.id,
            "subdomain": tenant.subdomain,
            "admin_user_id": admin.id,
            "whatsapp_number_id": whatsapp_number_id,
        }
    finally:
        db.close()
