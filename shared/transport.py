"""
Shared market data utilities for Finance MCP servers.

Top-level MCP servers import from here to get broker clients and the
fallback helper, then define their own tools.

Usage:
    from shared.transport import build_clients, with_fallback

    tt, yahoo = build_clients()

    @mcp.tool()
    async def get_quote(symbol: str) -> dict:
        return await with_fallback(tt, yahoo, "get_quote", symbol)

Environment variables:
    TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN
"""
from __future__ import annotations

import os

from data.brokers.tastytrade import TastytradeClient
from data.brokers.yahoo import YahooClient

_STALE = "Note: this analysis uses delayed data because the live feed is down."


def build_clients() -> tuple[TastytradeClient, YahooClient]:
    """Instantiate both broker clients from environment variables."""
    return (
        TastytradeClient(
            os.environ["TT_CLIENT_ID"],
            os.environ["TT_CLIENT_SECRET"],
            os.environ["TT_REFRESH_TOKEN"],
        ),
        YahooClient(),
    )


async def with_fallback(primary, fallback, method: str, *args, **kwargs):
    """Call method on primary broker; fall back to secondary on any exception."""
    try:
        return await getattr(primary, method)(*args, **kwargs)
    except Exception:
        result = await getattr(fallback, method)(*args, **kwargs)
        if isinstance(result, dict):
            result["_note"] = _STALE
        elif isinstance(result, list):
            return {"_note": _STALE, "items": result}
        return result
