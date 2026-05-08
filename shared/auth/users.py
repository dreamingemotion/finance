from __future__ import annotations

import secrets
import time

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def create_user(db, email: str, password: str) -> dict:
    uid = secrets.token_hex(16)
    await db.execute(
        "INSERT INTO users (id, email, password_hash, created_at) VALUES ($1, $2, $3, $4)",
        uid, email.lower(), _pwd.hash(password), int(time.time()),
    )
    return {"id": uid, "email": email.lower()}


async def get_user_by_email(db, email: str) -> dict | None:
    row = await db.fetchrow(
        "SELECT * FROM users WHERE email = $1 AND is_active = TRUE",
        email.lower(),
    )
    return dict(row) if row else None


async def get_user_by_id(db, user_id: str) -> dict | None:
    row = await db.fetchrow(
        "SELECT * FROM users WHERE id = $1 AND is_active = TRUE",
        user_id,
    )
    return dict(row) if row else None


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)
