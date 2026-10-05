"""Mostra os logins de um cliente e define uma senha nova pra um deles.

Rodar no terminal do VPS (Web console da Hostinger):
    docker exec -it crm-app python scripts/redefinir_senha.py

A senha é digitada escondida (não aparece na tela) e nunca fica salva em texto.
"""

import getpass

from app.auth import hash_password
from app.db import SessionLocal
from app.models import Tenant, User


def main() -> None:
    db = SessionLocal()
    try:
        tenants = db.query(Tenant).order_by(Tenant.name).all()
        print("\nClientes:")
        for i, t in enumerate(tenants, 1):
            print(f"  {i}) {t.name}  [{t.custom_domain or t.subdomain}]")
        tenant = tenants[int(input("\nNúmero do cliente: ").strip()) - 1]

        users = db.query(User).filter(User.tenant_id == tenant.id).order_by(User.role, User.name).all()
        print(f"\nLogins de {tenant.name}:")
        for i, u in enumerate(users, 1):
            status = "pendente" if u.pending_approval else ("ativo" if u.is_active else "desativado")
            print(f"  {i}) {u.email}  — {u.name} ({'Admin' if u.role == 'admin' else 'Vendedor'}, {status})")
        user = users[int(input("\nNúmero do login pra trocar a senha: ").strip()) - 1]

        password = getpass.getpass("Senha nova (não aparece ao digitar, mínimo 6): ")
        if len(password) < 6 or password != getpass.getpass("Repita a senha nova: "):
            print("\n❌ Senhas diferentes ou curtas demais. Nada foi alterado.")
            return
        user.password_hash = hash_password(password)
        user.is_active = True
        user.pending_approval = False
        db.commit()
        print(f"\n✅ Senha trocada. Login: {user.email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
