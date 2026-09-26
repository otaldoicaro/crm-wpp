from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import Tenant, User
from app.tenancy import get_tenant


def current_tenant(request: Request, db: Session = Depends(get_db)) -> Tenant:
    tenant = get_tenant(request, db)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant não encontrado para este domínio")
    return tenant


def current_user_required(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(current_tenant),
) -> User:
    user = get_current_user(request, db)
    if not user or user.tenant_id != tenant.id:
        raise HTTPException(status_code=401, detail="Não autenticado")
    return user
