"""
Research MCP server.

Provides structure-first SEC filing analysis via EDGAR + PageIndex.
Additional tool modules will be registered here as the server grows.

Usage:
    python -m research.server                                      # stdio
    python -m research.server --transport streamable-http          # no auth
    python -m research.server --transport streamable-http --require-auth

Environment variables:
  EDGAR_USER_AGENT    required by SEC, e.g. "MyApp contact@example.com"
  OPENROUTER_API_KEY  your OpenRouter API key
  OPENROUTER_BASE_URL https://openrouter.ai/api/v1 (default)
  GENERATION_MODEL    anthropic/claude-sonnet-4-6 (default)
  RESEARCH_WORKSPACE  PageIndex workspace directory (default: ./workspace)
  RESEARCH_HOST       bind host (default 0.0.0.0)
  RESEARCH_PORT       bind port (default 8093)
  RESEARCH_URL        public base URL for this server
  JWT_SECRET          shared with auth server (--require-auth only)
  AUTH_SERVER_URL     public URL of auth server (--require-auth only)

PageIndex uses LiteLLM internally. To route it through OpenRouter set:
  OPENAI_API_KEY  → same value as OPENROUTER_API_KEY
  OPENAI_BASE_URL → https://openrouter.ai/api/v1
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP
from research.tools.sec_filings import (
    batch_query          as _batch_query,
    delete_filing        as _delete_filing,
    get_filing_structure as _get_filing_structure,
    get_section          as _get_section,
    list_filings         as _list_filings,
    search_filing        as _search_filing,
    submit_filing        as _submit_filing,
)

_host = os.getenv("RESEARCH_HOST", "0.0.0.0")
_port = int(os.getenv("RESEARCH_PORT", "8093"))

mcp = FastMCP("finance-research", host=_host, port=_port)


@mcp.tool()
async def submit_filing(ticker: str, form_type: str, year: int) -> dict:
    """
    Fetch a filing from EDGAR and index it with PageIndex.

    Downloads the PDF for the most recent filing of form_type (e.g. "10-K")
    filed in the given year and returns a doc_id for use with all other tools.

    ticker can be a ticker symbol (e.g. "BLK") or a numeric CIK (e.g. "1364742").
    If the ticker lookup fails, the value is tried as a raw CIK automatically.

    Indexing a large filing may take several minutes on first call.
    Subsequent calls for the same filing return immediately from cache.
    """
    return await _submit_filing(ticker, form_type, year)


@mcp.tool()
async def get_filing_structure(doc_id: str) -> dict:
    """
    Return the full hierarchical section tree for a filing.

    Each node has title, node_id, page range, summary, and nested children.
    Use node_ids with get_section to retrieve the full text of any section.

    Call this first to orient yourself before fetching specific sections.
    """
    return await _get_filing_structure(doc_id)


@mcp.tool()
async def get_section(doc_id: str, node_id: str) -> dict:
    """
    Retrieve the full text of a section by node_id.

    Call get_filing_structure first to find node_ids. Returns section title,
    full text, word count, and page range.
    """
    return await _get_section(doc_id, node_id)


@mcp.tool()
async def search_filing(query: str, doc_id: str) -> dict:
    """
    Search a filing for sections relevant to a query.

    Navigates the filing's hierarchical structure, fetches the relevant
    sections, and returns cited passages with section and page info.
    For cross-company search, use batch_query instead.
    """
    return await _search_filing(query, doc_id)


@mcp.tool()
async def batch_query(query: str, doc_ids: list[str]) -> dict:
    """
    Search a query across multiple filings simultaneously.

    Runs in parallel. Use this to compare risk factors, disclosures,
    or financials across companies. Results are keyed by doc_id.
    """
    return await _batch_query(query, doc_ids)


@mcp.tool()
async def list_filings() -> list[dict]:
    """List all filings currently indexed in the local workspace."""
    return await _list_filings()


@mcp.tool()
async def delete_filing(doc_id: str) -> dict:
    """
    Permanently delete an indexed filing from the workspace.

    Does not delete the cached HTML file, only the search index.
    """
    return await _delete_filing(doc_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Research MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--require-auth", action="store_true")
    args = parser.parse_args()

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
    auth_url   = os.environ["AUTH_SERVER_URL"].rstrip("/")
    mcp_url    = os.environ.get("RESEARCH_URL", "").rstrip("/")
    resource_metadata_url = (
        f"{mcp_url}/.well-known/oauth-protected-resource" if mcp_url else ""
    )

    _mcp_asgi_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with _mcp_asgi_app.router.lifespan_context(_mcp_asgi_app):
            yield

    async def protected_resource_metadata(request: StarletteRequest):
        return JSONResponse({
            "resource":             f"{mcp_url}/mcp",
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
