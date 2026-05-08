"""
Finance MCP server entry point.

Aggregates tools from TastytradeClient (primary) and YahooClient (fallback)
and starts the server with the configured transport.

Usage:
    # stdio (default — for Claude Desktop local, Cursor, Zed, etc.)
    python transport.py

    # SSE HTTP (for remote/cloud clients)
    python transport.py --transport sse
    python transport.py --transport sse --host 0.0.0.0 --port 8000

    # SSE HTTP with OAuth 2.1 token validation
    python transport.py --transport sse --require-auth

Environment variables:
  Tastytrade:  TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN
  Auth (opt):  JWT_SECRET, AUTH_SERVER_URL

Dependencies:
    pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
import uvicorn

# Allow imports from this directory when run as a script
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

from data.brokers.tastytrade import TastytradeClient
from data.brokers.yahoo import YahooClient

_STALE = "Note: this analysis uses delayed data because the live feed is down."


async def _with_fallback(primary: object, fallback: object, method: str, *args, **kwargs):
    try:
        return await getattr(primary, method)(*args, **kwargs)
    except Exception:
        result = await getattr(fallback, method)(*args, **kwargs)
        if isinstance(result, dict):
            result["_note"] = _STALE
        elif isinstance(result, list):
            return {"_note": _STALE, "items": result}
        return result


def build_server() -> FastMCP:
    tt = TastytradeClient(
        os.environ["TT_CLIENT_ID"],
        os.environ["TT_CLIENT_SECRET"],
        os.environ["TT_REFRESH_TOKEN"],
    )
    yahoo = YahooClient()
    mcp = FastMCP("finance-data")

    # ---- Shared tools (TT primary, YF fallback) ----------------------------

    @mcp.tool()
    async def get_quote(symbol: str, instrument_type: str = "equity") -> dict:
        """
        Snapshot quote for a single instrument.

        instrument_type: equity | equity-option | future | future-option | cryptocurrency | index
        """
        return await _with_fallback(tt, yahoo, "get_quote", symbol, instrument_type)

    @mcp.tool()
    async def get_quotes(
        equities: list[str] | None = None,
        equity_options: list[str] | None = None,
        futures: list[str] | None = None,
        future_options: list[str] | None = None,
        cryptocurrencies: list[str] | None = None,
        indices: list[str] | None = None,
    ) -> list[dict]:
        """
        Snapshot quotes for multiple symbols across instrument types (limit: 100).

        Pass lists only for the types you need.
        """
        return await _with_fallback(
            tt, yahoo, "get_quotes",
            equities, equity_options, futures, future_options, cryptocurrencies, indices,
        )

    @mcp.tool()
    async def get_candles(
        symbols: list[str],
        period: str = "1d",
        from_date: str | None = None,
        duration_seconds: float = 10.0,
        regular_hours_only: bool = False,
    ) -> list[dict]:
        """
        OHLCV candles — live DXLink stream with Yahoo Finance fallback.

        period: 1m | 2m | 3m | 5m | 10m | 15m | 30m | 1h | 2h | 4h | 1d | 1w | 1mo
        from_date: ISO date YYYY-MM-DD; omit for full available history.
        duration_seconds: how long to collect after subscribing.
        regular_hours_only: True to exclude extended-hours bars.
        """
        return await _with_fallback(
            tt, yahoo, "get_candles",
            symbols, period, from_date, duration_seconds, regular_hours_only,
        )

    @mcp.tool()
    async def get_metrics(symbols: list[str]) -> list[dict]:
        """
        Market metrics: IV rank, IV percentile, HV (30/60/90-day), beta, earnings, dividends.
        """
        return await _with_fallback(tt, yahoo, "get_metrics", symbols)

    @mcp.tool()
    async def get_dividends(symbol: str) -> list[dict]:
        """Historical dividend events for a symbol."""
        return await _with_fallback(tt, yahoo, "get_dividends", symbol)

    @mcp.tool()
    async def get_earnings(symbol: str, start_date: str | None = None) -> dict:
        """
        Earnings history for a symbol.

        start_date: ISO date YYYY-MM-DD to filter results on or after that date.
        """
        return await _with_fallback(tt, yahoo, "get_earnings", symbol, start_date)

    @mcp.tool()
    async def get_option_chain(symbol: str) -> dict:
        """Full equity option chain keyed by expiration date (YYYY-MM-DD)."""
        return await _with_fallback(tt, yahoo, "get_option_chain", symbol)

    # ---- TT-only tools -----------------------------------------------------

    @mcp.tool()
    async def get_equity(symbol: str) -> dict:
        """Single equity instrument details."""
        return await tt.get_equity(symbol)

    @mcp.tool()
    async def get_equities(
        symbols: list[str],
        lendability: str | None = None,
        is_index: bool | None = None,
        is_etf: bool | None = None,
    ) -> list[dict]:
        """
        Multiple equity instruments.

        lendability: Easy To Borrow | Locate Required | Preborrow
        """
        return await tt.get_equities(symbols, lendability, is_index, is_etf)

    @mcp.tool()
    async def get_nested_option_chain(underlying_symbol: str) -> list[dict]:
        """Option chain in nested format: expirations → strikes → call/put pairs."""
        return await tt.get_nested_option_chain(underlying_symbol)

    @mcp.tool()
    async def get_futures(
        symbols: list[str] | None = None,
        product_codes: list[str] | None = None,
    ) -> list[dict]:
        """Futures contracts, filterable by symbols or product codes (e.g. ES, NQ)."""
        return await tt.get_futures(symbols, product_codes)

    @mcp.tool()
    async def get_future_option_chain(underlying_symbol: str) -> dict:
        """Option chain for a futures underlying, keyed by expiration date."""
        return await tt.get_future_option_chain(underlying_symbol)

    @mcp.tool()
    async def get_risk_free_rate() -> str:
        """Current risk-free rate used by Tastytrade for margin/options pricing."""
        return await tt.get_risk_free_rate()

    @mcp.tool()
    async def symbol_search(query: str) -> list[dict]:
        """Search for symbols matching a query string. Returns symbol and description."""
        return await tt.symbol_search(query)

    @mcp.tool()
    async def stream_quotes(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """Live bid/ask quotes collected for duration_seconds."""
        return await tt.stream_quotes(symbols, duration_seconds)

    @mcp.tool()
    async def stream_trades(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """Live trade prints collected for duration_seconds."""
        return await tt.stream_trades(symbols, duration_seconds)

    @mcp.tool()
    async def stream_candles(
        symbols: list[str],
        period: str = "1d",
        from_date: str | None = None,
        duration_seconds: float = 10.0,
        regular_hours_only: bool = False,
    ) -> list[dict]:
        """
        Historical + live OHLCV candles via DXLink (no fallback).

        period: 1m | 2m | 3m | 5m | 10m | 15m | 30m | 1h | 2h | 4h | 1d | 1w | 1mo
        from_date: ISO date YYYY-MM-DD; omit for full available history (~2001).
        duration_seconds: how long to collect after subscribing.
        regular_hours_only: True to filter extended-hours bars.
        """
        return await tt.stream_candles(symbols, period, from_date, duration_seconds, regular_hours_only)

    @mcp.tool()
    async def stream_greeks(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """Live Greeks (delta, gamma, theta, vega, rho, IV) for option symbols."""
        return await tt.stream_greeks(symbols, duration_seconds)

    @mcp.tool()
    async def stream_summaries(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """Day OHLC, previous close, open interest for symbols."""
        return await tt.stream_summaries(symbols, duration_seconds)

    @mcp.tool()
    async def stream_profiles(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """52-week high/low, trading status, halt status for symbols."""
        return await tt.stream_profiles(symbols, duration_seconds)

    @mcp.tool()
    async def stream_theo_prices(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """Theoretical price and Greeks for option symbols."""
        return await tt.stream_theo_prices(symbols, duration_seconds)

    @mcp.tool()
    async def stream_time_and_sales(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """Tick-level trade data for symbols."""
        return await tt.stream_time_and_sales(symbols, duration_seconds)

    @mcp.tool()
    async def stream_underlyings(symbols: list[str], duration_seconds: float = 5.0) -> list[dict]:
        """IV, put/call volumes, put-call ratio for underlying symbols."""
        return await tt.stream_underlyings(symbols, duration_seconds)

    # ---- YF-only tools -----------------------------------------------------

    @mcp.tool()
    def get_info(symbol: str) -> dict:
        """
        Full company profile and fundamentals: PE, EPS, market cap, sector,
        margins, analyst ratings, dividend info, 52-week range, and more.
        """
        return yahoo.get_info(symbol)

    @mcp.tool()
    def get_history(
        symbol: str,
        period: str = "1mo",
        interval: str = "1d",
        prepost: bool = False,
    ) -> dict:
        """
        OHLCV price history via Yahoo Finance.

        period:   1d | 5d | 1mo | 3mo | 6mo | 1y | 2y | 5y | 10y | ytd | max
        interval: 1m | 2m | 5m | 15m | 30m | 60m | 90m | 1h | 1d | 5d | 1wk | 1mo | 3mo
        prepost:  include pre/post-market data
        """
        return yahoo.get_history(symbol, period, interval, prepost)

    @mcp.tool()
    def get_financials(symbol: str) -> dict:
        """Annual income statement (revenue, gross profit, net income, EBITDA, etc.)."""
        return yahoo.get_financials(symbol)

    @mcp.tool()
    def get_balance_sheet(symbol: str) -> dict:
        """Annual balance sheet (assets, liabilities, equity, cash, debt, etc.)."""
        return yahoo.get_balance_sheet(symbol)

    @mcp.tool()
    def get_cashflow(symbol: str) -> dict:
        """Annual cash flow statement (operating, investing, financing activities)."""
        return yahoo.get_cashflow(symbol)

    @mcp.tool()
    def get_splits(symbol: str) -> dict:
        """Historical stock splits."""
        return yahoo.get_splits(symbol)

    @mcp.tool()
    def get_recommendations(symbol: str) -> dict:
        """Most recent analyst buy/sell/hold recommendations (last 20)."""
        return yahoo.get_recommendations(symbol)

    @mcp.tool()
    def get_news(symbol: str) -> dict:
        """Recent news articles (up to 10) for a symbol."""
        return yahoo.get_news(symbol)

    @mcp.tool()
    def get_option_expirations(symbol: str) -> dict:
        """Available options expiration dates for a symbol."""
        return yahoo.get_option_expirations(symbol)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Finance MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host for SSE transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help=(
            "Validate Bearer JWT tokens on every request. "
            "Requires JWT_SECRET and AUTH_SERVER_URL env vars."
        ),
    )
    args = parser.parse_args()

    mcp = build_server()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # SSE transport
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    if not args.require_auth:
        mcp.run(transport="sse")
        return

    # ---- Auth-protected SSE ------------------------------------------------
    from starlette.applications import Starlette
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    from auth.middleware import BearerTokenMiddleware

    jwt_secret = os.environ["JWT_SECRET"]
    auth_url = os.environ["AUTH_SERVER_URL"].rstrip("/")

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

    app = Starlette(routes=[
        Route("/.well-known/oauth-authorization-server", oauth_discovery),
        Mount("/", app=mcp.sse_app()),
    ])
    app.add_middleware(BearerTokenMiddleware, jwt_secret=jwt_secret)

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    anyio.run(server.serve)


if __name__ == "__main__":
    main()
