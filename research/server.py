"""
Research MCP server.

Provides structure-first SEC filing analysis via EDGAR + PageIndex.
Additional tool modules will be registered here as the server grows.

Usage:
    python -m research.server                                      # stdio
    python -m research.server --transport streamable-http          # no auth
    python -m research.server --transport streamable-http --require-auth

Environment variables:
  EDGAR_USER_AGENT          required by SEC, e.g. "MyApp contact@example.com"
  OPENROUTER_API_KEY        your OpenRouter API key
  OPENROUTER_BASE_URL       https://openrouter.ai/api/v1 (default)
  GENERATION_MODEL          anthropic/claude-sonnet-4-6 (default)
  RESEARCH_WORKSPACE        workspace directory (default: ./workspace)
  RESEARCH_HOST             bind host (default 0.0.0.0)
  RESEARCH_PORT             bind port (default 8093)
  RESEARCH_URL              public base URL for this server
  JWT_SECRET                shared with auth server (--require-auth only)
  AUTH_SERVER_URL           public URL of auth server (--require-auth only)
  TT_CLIENT_ID              tastytrade OAuth client ID
  TT_CLIENT_SECRET          tastytrade OAuth client secret
  TT_REFRESH_TOKEN          tastytrade OAuth refresh token

PageIndex uses LiteLLM internally. To route it through OpenRouter set:
  OPENAI_API_KEY  → same value as OPENROUTER_API_KEY
  OPENAI_BASE_URL → https://openrouter.ai/api/v1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)

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
from research.tools.market_data import (
    get_quote           as _get_quote,
    get_snapshot        as _get_snapshot,
    get_bars            as _get_bars,
    get_full_timeframe  as _get_full_timeframe,
)
from research.tools.knowledge import (
    search_knowledge          as _search_knowledge,
    list_knowledge_categories as _list_knowledge_categories,
    list_knowledge_documents  as _list_knowledge_documents,
    get_knowledge_document    as _get_knowledge_document,
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


@mcp.tool()
async def get_quote(symbol: str) -> dict:
    """
    Fetch a real-time quote for an equity symbol.

    Returns price, bid, ask, mark, day open/high/low/close, volume,
    data_source ("primary" = tastytrade, "secondary" = yfinance),
    and stale (true if the quote predates the most recent market close).
    """
    return await _get_quote(symbol)


@mcp.tool()
async def get_snapshot(symbol: str) -> dict:
    """
    Fetch a full market snapshot for an equity symbol.

    Combines real-time quote with extended metrics: P/E ratio (tastytrade),
    P/B ratio (yfinance), IV rank, 30/60-day historical volatility, beta,
    market cap, dividend yield, and borrow rate.

    data_source reflects whether tastytrade (primary) or yfinance
    (secondary) supplied the quote and metrics.  P/B is always from yfinance.
    """
    return await _get_snapshot(symbol)


@mcp.tool()
async def get_bars(symbol: str, period: str, interval: str) -> dict:
    """
    Fetch OHLCV bars for a single timeframe.

    For "full timeframe", "full timeframe continuity", or any multi-timeframe
    request, use get_full_timeframe instead — do NOT call this tool multiple times.

    period   — look-back window: 1d 5d 1mo 3mo 6mo 1y 2y 5y 10y
    interval — bar width:        1m 5m 15m 30m 1h 1d 1wk 1mo

    Returns bars (list of {time, open, high, low, close, volume}),
    bar_count, data_source, last_bar_stale, and suppress_time_gaps.
    Bars are sorted oldest-first so they can be passed directly to a
    chart library.  tastytrade DXLink is primary; yfinance is the fallback.

    Always render the result as a candlestick chart using the OHLCV fields,
    unless the user explicitly requests a different chart type.
    When suppress_time_gaps is true, use a categorical or sequential index
    x-axis (not a datetime axis) so overnight and weekend gaps are not shown.
    """
    return await _get_bars(symbol, period, interval)


@mcp.tool()
async def get_full_timeframe(symbol: str, charts: list[dict] | None = None) -> dict:
    """
    ALWAYS call this tool — never get_bars — when the user wants a
    multi-timeframe overview, continuity analysis, or asks to see a symbol
    across multiple timeframes at once. Do NOT call get_bars multiple times
    as a substitute.

    charts: optional list of {"period": str, "interval": str} dicts to
    override the default timeframes. Omit for the four standard timeframes
    arranged in a 2×2 grid (top-left → top-right → bottom-left → bottom-right):
      top-left:     2-Month Daily   — short-term trend and recent price action
      top-right:    2-Year Weekly   — intermediate trend and swing structure
      bottom-left:  3-Day 60 Min    — intraday detail and entry/exit context
      bottom-right: 3-Year Monthly  — long-term trend and major structure

    Each entry in charts[] has label, symbol, period, interval, bar_count,
    data_source, last_bar_stale, and bars (OHLCV list, oldest-first).

    Each chart entry includes render_order (0-based integer) and grid_position
    ("top-left", "top-right", "bottom-left", "bottom-right") fields.

    Rendering rules (follow exactly — do not override):
    - Render as candlestick charts in a 2×2 grid.
    - Sort charts by render_order and place each at its grid_position.
      Never reorder by timeframe length or any other logic.
    - Use each chart's label field verbatim as the chart title. Do not
      rename or substitute (e.g. do not replace "60 Min" with "Hourly").
    - When a chart's suppress_time_gaps is true, use a categorical or
      sequential index x-axis so overnight and weekend gaps are not shown.
    - Do not use line charts unless the user explicitly asks.
    """
    return await _get_full_timeframe(symbol, charts)


@mcp.tool()
async def search_knowledge(
    query: str,
    categories: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """
    Semantic search over the finance knowledge base.

    Embeds the query and returns the most similar knowledge chunks ranked by
    cosine similarity.  Use this to pull in domain knowledge — strategy notes,
    macro context, risk frameworks — before or during analysis.

    categories: optional list of category names to restrict results.
        Common values: risk, market_risk, macro, strategy, technical,
        sentiment, earnings, sector, valuation, options, inference, methodology.
        Call list_knowledge_categories to see all available categories and counts.

    Returns chunks with content, source document title, source_url, categories,
    and similarity score.
    """
    return await _search_knowledge(query, categories=categories, limit=limit)


@mcp.tool()
async def list_knowledge_categories() -> list[dict]:
    """
    List all categories in the knowledge base with chunk counts.

    Returns seeded categories (risk, macro, strategy, etc.) and any
    auto-discovered categories.  Use category names with search_knowledge
    to scope queries to a specific domain.
    """
    return await _list_knowledge_categories()


@mcp.tool()
async def list_knowledge_documents() -> list[dict]:
    """
    List all documents ingested into the knowledge base.

    Returns document id, title, source_url, creation date, and chunk count.
    Use document ids with get_knowledge_document to retrieve full chunk text.
    """
    return await _list_knowledge_documents()


@mcp.tool()
async def get_knowledge_document(document_id: int) -> dict:
    """
    Retrieve all chunks for a specific knowledge document.

    Returns the document title, source_url, and every chunk with its content
    and categories.  Use search_knowledge when you want targeted passages
    instead of an entire document.
    """
    return await _get_knowledge_document(document_id)


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
