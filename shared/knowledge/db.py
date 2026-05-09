"""
asyncpg connection pool and schema init for the knowledge store.

Database: pointed at by KNOWLEDGE_DATABASE_URL (e.g. finance DB on 10.0.0.139)
Schema:   knowledge — documents, chunks, categories, chunk_categories
Vector:   pgvector extension, 3072-dim embeddings (text-embedding-3-large)
          No vector index — pgvector IVFFlat/HNSW top out at 2000 dims; sequential
          scan is fine for a personal knowledge base (thousands of chunks).

Environment variables:
  KNOWLEDGE_DATABASE_URL  postgresql://user:pass@10.0.0.139:5432/finance
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg

_pool: asyncpg.Pool | None = None

SEEDED_CATEGORIES: dict[str, str] = {
    "risk":        "General risk factors and warnings",
    "market_risk": "Market-specific risks: concentration, liquidity, breadth, fragility",
    "macro":       "Macroeconomic factors: Fed policy, rates, inflation, GDP",
    "strategy":    "Trading and investment strategies",
    "technical":   "Technical analysis, chart patterns, indicators",
    "sentiment":   "Market sentiment, fear/greed, investor positioning",
    "earnings":    "Earnings reports, guidance, analyst estimates",
    "sector":      "Sector rotation and sector-specific analysis",
    "valuation":   "Valuations: PE ratios, multiples, fair value, spreads",
    "options":     "Options-specific: VIX, implied volatility, skew, positioning",
    "inference":   "Non-obvious investable inferences, causal chains, and comparisons derived from analysis",
    "methodology": "Analytical frameworks, models, and methodologies used in the research",
}

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS knowledge;

CREATE TABLE IF NOT EXISTS knowledge.documents (
    id           SERIAL PRIMARY KEY,
    title        TEXT NOT NULL,
    source_url   TEXT,
    raw_content  TEXT NOT NULL,
    content_hash TEXT UNIQUE,
    uploaded_by  TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge.categories (
    name        TEXT PRIMARY KEY,
    description TEXT,
    is_seeded   BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS knowledge.chunks (
    id          SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES knowledge.documents(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    embedding   vector(3072),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge.chunk_categories (
    chunk_id    INTEGER NOT NULL REFERENCES knowledge.chunks(id) ON DELETE CASCADE,
    category    TEXT    NOT NULL REFERENCES knowledge.categories(name),
    PRIMARY KEY (chunk_id, category)
);

"""


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(os.environ["KNOWLEDGE_DATABASE_URL"])
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db() -> None:
    """
    Create schema and seed categories using a one-off connection.
    Safe to call before the server starts (does not touch the pool).

    Requires the pgvector extension to be available on the Postgres server.
    If CREATE EXTENSION fails due to permissions, run it manually as a superuser:
      CREATE EXTENSION IF NOT EXISTS vector;
    """
    conn = await asyncpg.connect(os.environ["KNOWLEDGE_DATABASE_URL"])
    try:
        await conn.execute(_SCHEMA)
        # Migrations: add columns to existing installs that predate them.
        await conn.execute("""
            ALTER TABLE knowledge.documents
            ADD COLUMN IF NOT EXISTS content_hash TEXT;
        """)
        await conn.execute("""
            ALTER TABLE knowledge.documents
            ADD COLUMN IF NOT EXISTS uploaded_by TEXT;
        """)
        await conn.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'documents_content_hash_key'
                ) THEN
                    ALTER TABLE knowledge.documents
                    ADD CONSTRAINT documents_content_hash_key UNIQUE (content_hash);
                END IF;
            END $$;
        """)
        for name, description in SEEDED_CATEGORIES.items():
            await conn.execute(
                """
                INSERT INTO knowledge.categories (name, description, is_seeded)
                VALUES ($1, $2, TRUE)
                ON CONFLICT (name) DO NOTHING
                """,
                name, description,
            )
    finally:
        await conn.close()


@asynccontextmanager
async def get_db():
    """Async context manager that yields a pooled connection."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        yield conn
