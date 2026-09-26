from __future__ import annotations

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, SESSION_COOKIE_NAME
from app.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(SECRET_KEY, salt="crm-session")

SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 dias


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return pwd_context.verify(raw, hashed)


def create_session_token(user_id: str, tenant_id: str) -> str:
    return serializer.dumps({"user_id": user_id, "tenant_id": tenant_id})


def read_session_token(token: str) -> dict | None:
    try:
        return serializer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    data = read_session_token(token)
    if not data:
        return None
    user = db.get(User, data["user_id"])
    if not user or not user.is_active:
        return None
    return user
