"""
Postgres connection pool for the auth server.

Requires DATABASE_URL environment variable, e.g.:
    postgresql://user:password@10.0.0.139:5432/finance_auth
"""

from __future__ import annotations

import os

import asyncpg

_POOL: asyncpg.Pool | None = None

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        email         TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at    BIGINT NOT NULL,
        is_active     BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_codes (
        code                  TEXT PRIMARY KEY,
        client_id             TEXT NOT NULL,
        user_id               TEXT NOT NULL,
        redirect_uri          TEXT NOT NULL,
        scope                 TEXT NOT NULL,
        code_challenge        TEXT NOT NULL,
        code_challenge_method TEXT NOT NULL DEFAULT 'S256',
        expires_at            BIGINT NOT NULL,
        used                  BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refresh_tokens (
        token      TEXT PRIMARY KEY,
        client_id  TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        scope      TEXT NOT NULL,
        expires_at BIGINT NOT NULL,
        revoked    BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
]


async def init_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            os.environ["DATABASE_URL"],
            min_size=2,
            max_size=10,
        )
    return _POOL


async def init_db() -> None:
    pool = await init_pool()
    async with pool.acquire() as conn:
        for stmt in _SCHEMA:
            await conn.execute(stmt)


async def get_db():
    """FastAPI dependency — yields an asyncpg connection from the pool."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        yield conn
