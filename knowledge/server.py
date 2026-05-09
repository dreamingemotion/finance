"""
Knowledge MCP server.

Ingests documents uploaded via the Claude app and exposes semantic search
and category-based retrieval over the stored knowledge base.

Usage:
    python -m knowledge.server                                      # stdio
    python -m knowledge.server --transport streamable-http          # no auth
    python -m knowledge.server --transport streamable-http --require-auth

Environment variables:
  KNOWLEDGE_DATABASE_URL  postgresql://user:pass@10.0.0.139:5432/finance
  OPENROUTER_API_KEY      your OpenRouter API key
  OPENROUTER_BASE_URL     https://openrouter.ai/api/v1 (default)
  GENERATION_MODEL        anthropic/claude-sonnet-4-6 (default)
  EMBEDDING_MODEL         openai/text-embedding-3-large (default)
  MCP_HOST                bind host (default 0.0.0.0)
  MCP_PORT                bind port (default 8092)
  JWT_SECRET              shared with auth server (--require-auth only)
  AUTH_SERVER_URL         public URL of auth server (--require-auth only)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP
from shared.knowledge.db import close_pool, get_db, init_db
from shared.knowledge.retriever import KnowledgeRetriever
from knowledge.ingest import ingest_document as _ingest

_host = os.getenv("MCP_HOST", "0.0.0.0")
_port = int(os.getenv("MCP_PORT", "8092"))

mcp = FastMCP("finance-knowledge", host=_host, port=_port)


@mcp.tool()
async def ingest_document(
    title: str,
    content: str,
    source_url: str | None = None,
) -> dict:
    """
    Ingest a document into the knowledge base.

    Extracts discrete insight chunks using Claude, embeds them for semantic
    search, and stores everything with category tags.

    Call this after reading a file the user has uploaded. Pass the full text
    as content and a descriptive title. source_url is optional (useful for
    web articles).
    """
    async with get_db() as db:
        return await _ingest(db, title, content, source_url)


@mcp.tool()
async def search_knowledge(
    query: str,
    categories: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Semantic search over the knowledge base.

    Returns the most relevant insight chunks for the query. Optionally
    filter to specific categories (e.g. ["risk", "market_risk"]).
    Use this during analysis to surface relevant stored knowledge.
    """
    async with get_db() as db:
        return await KnowledgeRetriever(db).search(query, categories, limit)


@mcp.tool()
async def get_chunks_by_category(category: str, limit: int = 20) -> list[dict]:
    """
    Retrieve all chunks tagged with a category, most recent first.

    Useful for a broad review of everything stored under a topic
    (e.g. all "strategy" insights).
    """
    async with get_db() as db:
        return await KnowledgeRetriever(db).get_by_category(category, limit)


@mcp.tool()
async def list_categories() -> list[dict]:
    """
    List all categories with chunk counts.

    Includes both seeded categories (risk, macro, strategy, etc.) and any
    new categories Claude discovered during ingestion.
    """
    async with get_db() as db:
        return await KnowledgeRetriever(db).list_categories()


@mcp.tool()
async def list_documents() -> list[dict]:
    """List all ingested documents with titles, sources, and chunk counts."""
    async with get_db() as db:
        return await KnowledgeRetriever(db).list_documents()


@mcp.tool()
async def get_document(document_id: int) -> dict:
    """
    Retrieve a full document and all its extracted chunks by ID.

    Use list_documents() to find document IDs.
    """
    async with get_db() as db:
        result = await KnowledgeRetriever(db).get_document(document_id)
        if result is None:
            return {"error": f"No document with id {document_id}"}
        return result


@mcp.tool()
async def delete_document(document_id: int) -> dict:
    """
    Permanently delete a document and all its chunks. Cannot be undone.
    """
    async with get_db() as db:
        tag = await db.execute(
            "DELETE FROM knowledge.documents WHERE id = $1", document_id
        )
        if tag.split()[-1] == "0":
            return {"error": f"No document with id {document_id}"}
        return {"deleted": True, "document_id": document_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--require-auth", action="store_true")
    args = parser.parse_args()

    asyncio.run(init_db())

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    if not args.require_auth:
        mcp.run(transport="streamable-http")
        return

    # ---- Auth-protected streamable-http ------------------------------------
    import anyio
    import uvicorn
    from contextlib import asynccontextmanager
    from starlette.applications import Starlette
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route
    from shared.auth.middleware import BearerTokenMiddleware

    jwt_secret = os.environ["JWT_SECRET"]
    auth_url = os.environ["AUTH_SERVER_URL"].rstrip("/")

    @asynccontextmanager
    async def lifespan(app):
        yield
        await close_pool()

    async def oauth_discovery(request: StarletteRequest):
        return JSONResponse({
            "issuer": auth_url,
            "authorization_endpoint": f"{auth_url}/authorize",
            "token_endpoint": f"{auth_url}/token",
            "revocation_endpoint": f"{auth_url}/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        })

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/.well-known/oauth-authorization-server", oauth_discovery),
            Mount("/", app=mcp.streamable_http_app()),
        ],
    )
    app.add_middleware(BearerTokenMiddleware, jwt_secret=jwt_secret)

    config = uvicorn.Config(app, host=_host, port=_port, log_level="info")
    server = uvicorn.Server(config)
    anyio.run(server.serve)


if __name__ == "__main__":
    main()
