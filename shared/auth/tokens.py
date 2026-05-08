from __future__ import annotations

import secrets
import time

import jwt

ACCESS_TOKEN_EXPIRE = 15 * 60           # 15 minutes
REFRESH_TOKEN_EXPIRE = 30 * 24 * 3600  # 30 days


def make_access_token(user_id: str, email: str, scope: str, secret: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "scope": scope,
            "iat": now,
            "exp": now + ACCESS_TOKEN_EXPIRE,
        },
        secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, secret: str) -> dict:
    """Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure."""
    return jwt.decode(token, secret, algorithms=["HS256"])


async def create_refresh_token(db, client_id: str, user_id: str, scope: str) -> str:
    token = secrets.token_urlsafe(48)
    await db.execute(
        "INSERT INTO refresh_tokens (token, client_id, user_id, scope, expires_at)"
        " VALUES ($1, $2, $3, $4, $5)",
        token, client_id, user_id, scope, int(time.time()) + REFRESH_TOKEN_EXPIRE,
    )
    return token


async def rotate_refresh_token(db, old_token: str) -> tuple[dict, str] | None:
    """
    Validate old_token, revoke it, and issue a replacement in a single transaction.
    Returns (old_row_dict, new_token) or None if the token is invalid/expired/revoked.
    """
    now = int(time.time())
    async with db.transaction():
        row = await db.fetchrow(
            "SELECT * FROM refresh_tokens"
            " WHERE token = $1 AND revoked = FALSE AND expires_at > $2",
            old_token, now,
        )
        if row is None:
            return None
        row = dict(row)
        await db.execute(
            "UPDATE refresh_tokens SET revoked = TRUE WHERE token = $1", old_token
        )
        new_token = secrets.token_urlsafe(48)
        await db.execute(
            "INSERT INTO refresh_tokens (token, client_id, user_id, scope, expires_at)"
            " VALUES ($1, $2, $3, $4, $5)",
            new_token, row["client_id"], row["user_id"], row["scope"],
            now + REFRESH_TOKEN_EXPIRE,
        )
    return row, new_token
