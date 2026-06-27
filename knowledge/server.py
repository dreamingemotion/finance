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
  KNOWLEDGE_HOST          bind host (default 0.0.0.0)
  KNOWLEDGE_PORT          bind port (default 8092)
  KNOWLEDGE_URL           public base URL, e.g. https://mcp.unfolding.in/servers/finance/knowledge
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
from knowledge.ingest import extract_chunks as _extract_chunks
from knowledge.ingest import commit_document as _commit_document
from knowledge.ingest import ingest_document as _ingest
from shared.knowledge.embedder import embed as _embed

_host = os.getenv("KNOWLEDGE_HOST", "0.0.0.0")
_port = int(os.getenv("KNOWLEDGE_PORT", "8092"))

mcp = FastMCP("finance-knowledge", host=_host, port=_port)


@mcp.tool()
async def preview_ingest(
    title: str,
    content: str,
) -> dict:
    """
    Step 1 of 2: extract chunks from a document and return them for review.
    Does NOT write anything to the database.

    Runs a factual extraction pass and, if the document contains analysis or
    commentary, a second inference pass (methodologies, causal chains,
    comparative patterns, open-ended inferences).

    Each chunk includes:
      - content: the extracted insight
      - categories: 1-3 category tags
      - pass: "factual" or "inference"

    Present the chunks to the user. They can approve as-is, ask you to edit,
    remove, or reword individual chunks. Once approved, call commit_ingest
    with the final chunk list to embed and save.
    """
    return await _extract_chunks(title, content)


@mcp.tool()
async def commit_ingest(
    title: str,
    content: str,
    chunks: list[dict],
    source_url: str | None = None,
    overwrite: bool = False,
) -> dict:
    """
    Step 2 of 2: embed and save approved chunks to the knowledge base.

    Pass the chunks list exactly as returned by preview_ingest, after any
    edits the user requested. The "pass" field on each chunk is used for
    reporting and ignored during storage.

    content must be the same original text passed to preview_ingest —
    it is stored as raw_content and used for duplicate detection.

    If the content was already ingested, returns {"duplicate": true, ...}.
    Call again with overwrite=True to replace the existing document.

    IMPORTANT: only call this tool when the user explicitly says to save,
    commit, or confirm using those words. Never call it automatically after:
    - answering a question or defining a term
    - making an edit or reword
    - the user selecting one of several reword options
    - any other mid-review action
    After every edit, display the full updated chunk list and wait for a
    separate, explicit commit instruction before calling this tool.
    """
    async with get_db() as db:
        return await _commit_document(db, title, content, chunks, source_url, overwrite)


@mcp.tool()
async def ingest_document(
    title: str,
    content: str,
    source_url: str | None = None,
    overwrite: bool = False,
) -> dict:
    """
    Single-call ingestion — skips the review step.

    Prefer preview_ingest → commit_ingest when the user should approve
    chunks before they are saved. Use this only when review is not needed.
    """
    async with get_db() as db:
        return await _ingest(db, title, content, source_url, overwrite)


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
async def add_chunk(
    document_id: int,
    content: str,
    categories: list[str],
) -> dict:
    """
    Add a new chunk to an existing document.

    Embeds the content and saves it under the given document. Useful for
    manually adding insights that the extraction passes missed, or for
    adding a definition or clarification chunk during review.
    """
    async with get_db() as db:
        exists = await db.fetchval(
            "SELECT id FROM knowledge.documents WHERE id = $1", document_id
        )
        if exists is None:
            return {"error": f"No document with id {document_id}"}

        vector = (await _embed([content]))[0]
        vec_str = f"[{','.join(str(v) for v in vector)}]"

        new_categories: list[str] = []
        async with db.transaction():
            chunk_id = await db.fetchval(
                """
                INSERT INTO knowledge.chunks (document_id, content, embedding)
                VALUES ($1, $2, $3::vector)
                RETURNING id
                """,
                document_id, content, vec_str,
            )
            for cat in categories:
                cat = cat.lower().strip().replace(" ", "_")
                await db.execute(
                    """
                    INSERT INTO knowledge.categories (name, is_seeded)
                    VALUES ($1, FALSE)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    cat,
                )
                await db.execute(
                    "INSERT INTO knowledge.chunk_categories (chunk_id, category) VALUES ($1, $2)",
                    chunk_id, cat,
                )
                new_categories.append(cat)

        return {
            "added": True,
            "chunk_id": chunk_id,
            "document_id": document_id,
            "categories": new_categories,
        }


@mcp.tool()
async def update_chunk(
    chunk_id: int,
    content: str | None = None,
    categories: list[str] | None = None,
) -> dict:
    """
    Edit a specific chunk in place. At least one of content or categories must be provided.

    - content: replaces the chunk text and re-embeds it automatically
    - categories: replaces all existing category tags for this chunk

    Use get_document(document_id) to find chunk IDs. Changes are immediate —
    show the proposed update to the user and wait for explicit confirmation
    before calling this tool.
    """
    if content is None and categories is None:
        return {"error": "Provide at least one of content or categories"}

    async with get_db() as db:
        exists = await db.fetchval(
            "SELECT id FROM knowledge.chunks WHERE id = $1", chunk_id
        )
        if exists is None:
            return {"error": f"No chunk with id {chunk_id}"}

        async with db.transaction():
            if content is not None:
                vector = (await _embed([content]))[0]
                vec_str = f"[{','.join(str(v) for v in vector)}]"
                await db.execute(
                    "UPDATE knowledge.chunks SET content = $1, embedding = $2::vector WHERE id = $3",
                    content, vec_str, chunk_id,
                )

            if categories is not None:
                await db.execute(
                    "DELETE FROM knowledge.chunk_categories WHERE chunk_id = $1", chunk_id
                )
                for cat in categories:
                    cat = cat.lower().strip().replace(" ", "_")
                    await db.execute(
                        """
                        INSERT INTO knowledge.categories (name, is_seeded)
                        VALUES ($1, FALSE)
                        ON CONFLICT (name) DO NOTHING
                        """,
                        cat,
                    )
                    await db.execute(
                        "INSERT INTO knowledge.chunk_categories (chunk_id, category) VALUES ($1, $2)",
                        chunk_id, cat,
                    )

        return {
            "updated": True,
            "chunk_id": chunk_id,
            "content_updated": content is not None,
            "categories_updated": categories is not None,
        }


@mcp.tool()
async def delete_chunk(chunk_id: int) -> dict:
    """
    Permanently delete a single chunk without affecting the rest of the document.

    Use get_document(document_id) to find chunk IDs. Cannot be undone.
    """
    async with get_db() as db:
        tag = await db.execute(
            "DELETE FROM knowledge.chunks WHERE id = $1", chunk_id
        )
        if tag.split()[-1] == "0":
            return {"error": f"No chunk with id {chunk_id}"}
        return {"deleted": True, "chunk_id": chunk_id}


@mcp.tool()
async def update_document(
    document_id: int,
    title: str | None = None,
    source_url: str | None = None,
) -> dict:
    """
    Update a document's title or source_url. At least one must be provided.

    Does not affect chunks or embeddings. To update content, delete and re-ingest.
    """
    if title is None and source_url is None:
        return {"error": "Provide at least one of title or source_url"}

    async with get_db() as db:
        exists = await db.fetchval(
            "SELECT id FROM knowledge.documents WHERE id = $1", document_id
        )
        if exists is None:
            return {"error": f"No document with id {document_id}"}

        if title is not None and source_url is not None:
            await db.execute(
                "UPDATE knowledge.documents SET title = $1, source_url = $2 WHERE id = $3",
                title, source_url, document_id,
            )
        elif title is not None:
            await db.execute(
                "UPDATE knowledge.documents SET title = $1 WHERE id = $2",
                title, document_id,
            )
        else:
            await db.execute(
                "UPDATE knowledge.documents SET source_url = $1 WHERE id = $2",
                source_url, document_id,
            )

        return {
            "updated": True,
            "document_id": document_id,
            "title_updated": title is not None,
            "source_url_updated": source_url is not None,
        }


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
    mcp_url = os.environ.get("KNOWLEDGE_URL", "").rstrip("/")
    resource_metadata_url = f"{mcp_url}/.well-known/oauth-protected-resource" if mcp_url else ""

    # Build the MCP ASGI app first so its lifespan can be referenced below.
    # When mounted inside a wrapper Starlette app, Starlette does NOT
    # automatically call the inner app's lifespan, so we chain it explicitly.
    # Without this the StreamableHTTPSessionManager's task group is never
    # initialized, causing RuntimeError on the first request.
    _mcp_asgi_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with _mcp_asgi_app.router.lifespan_context(_mcp_asgi_app):
            yield
        await close_pool()

    async def protected_resource_metadata(request: StarletteRequest):
        return JSONResponse({
            "resource": f"{mcp_url}/mcp",
            "authorization_servers": [auth_url],
        })

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
            Mount("/", app=_mcp_asgi_app),
        ],
    )
    app.add_middleware(
        BearerTokenMiddleware,
        jwt_secret=jwt_secret,
        resource_metadata_url=resource_metadata_url,
    )

    config = uvicorn.Config(app, host=_host, port=_port, log_level="info")
    server = uvicorn.Server(config)
    anyio.run(server.serve)


if __name__ == "__main__":
    main()
