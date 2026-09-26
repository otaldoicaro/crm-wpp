"""Cria um tenant de demonstração para testar localmente.

Uso:
    source .venv/bin/activate
    python seed.py
"""

from app.auth import hash_password
from app.db import Base, SessionLocal, engine
from app.models import PipelineStage, Tenant, User, WhatsAppNumber

Base.metadata.create_all(bind=engine)

db = SessionLocal()

SUBDOMAIN = "demo"
ADMIN_EMAIL = "admin@demo.com"
ADMIN_PASSWORD = "demo1234"

tenant = db.query(Tenant).filter(Tenant.subdomain == SUBDOMAIN).first()
if not tenant:
    tenant = Tenant(name="Cliente Demo", subdomain=SUBDOMAIN)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    print(f"Tenant criado: {tenant.name} ({tenant.id})")

    stages = [
        ("Novo", 0, "", False, False),
        ("Em atendimento", 1, "", False, False),
        ("Qualificado", 2, "Qualified", False, False),
        ("Ganho", 3, "Purchase", True, False),
        ("Perdido", 4, "", False, True),
    ]
    for name, order, event_name, is_won, is_lost in stages:
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

    admin = User(
        tenant_id=tenant.id,
        name="Admin",
        email=ADMIN_EMAIL,
        password_hash=hash_password(ADMIN_PASSWORD),
        role="admin",
    )
    db.add(admin)

    agent2 = User(
        tenant_id=tenant.id,
        name="Atendente 2",
        email="atendente2@demo.com",
        password_hash=hash_password("demo1234"),
        role="agent",
    )
    db.add(agent2)

    number = WhatsAppNumber(
        tenant_id=tenant.id,
        label="Número principal (teste)",
        phone_number="+5511999990000",
        waba_phone_number_id="COLOQUE_O_PHONE_NUMBER_ID_DO_META",
        verify_token="dev-verify-token",
    )
    db.add(number)

    db.commit()
    print(f"WhatsAppNumber.id para usar na URL da ponte /go/{tenant.id}/<whatsapp_number_id>: {number.id}")
else:
    print(f"Tenant '{SUBDOMAIN}' já existe ({tenant.id}). Nada foi criado.")

print()
print("Acesse: http://demo.localhost:8000/login  (ou http://localhost:8000/login?tenant=demo)")
print(f"Login: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
print(f"Webhook de formulário de site: POST http://localhost:8000/webhooks/site-form/{tenant.id}")

db.close()
