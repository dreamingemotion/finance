"""
yfinance market data provider (secondary / fallback).
Uses the shared YahooClient for consistency with the primary tastytrade provider.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from shared.data.brokers.yahoo import YahooClient

from .base import MarketDataProvider

# DXFeed/Tastytrade symbols that map to different tickers on Yahoo Finance.
# Add entries here whenever a primary symbol would resolve to the wrong security.
_YF_SYMBOL_MAP: dict[str, str] = {
    "IXIC": "^IXIC",   # Nasdaq Composite (bare IXIC unknown to Yahoo Finance)
    "SPX":  "^GSPC",   # S&P 500 index
    "VIX":  "^VIX",    # CBOE Volatility Index
}

_PERIOD_DAYS: dict[str, int | None] = {
    "1d":  1,   "3d":  3,   "5d":  5,   "1mo": 30,  "2mo": 60,
    "3mo": 90,  "6mo": 180, "1y":  365, "2y":  730, "3y":  1095,
    "5y":  1825, "10y": 3650, "max": None,
}

# yfinance-style interval → YahooClient candle period (TT-style)
_YF_TO_YC: dict[str, str] = {
    "1wk": "1w",
}


def _from_date(period: str) -> str | None:
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.date().isoformat()


def _candles_to_bars(candles: list[dict]) -> list[dict]:
    bars = []
    for c in candles:
        t = c.get("time", 0)
        ts = datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat() if isinstance(t, (int, float)) else str(t)
        bars.append({
            "time":   ts,
            "open":   float(c["open"]),
            "high":   float(c["high"]),
            "low":    float(c["low"]),
            "close":  float(c["close"]),
            "volume": float(c["volume"]) if c.get("volume") is not None else None,
        })
    bars.sort(key=lambda b: b["time"])
    return bars


class YFinanceProvider(MarketDataProvider):

    def __init__(self) -> None:
        self._client = YahooClient()

    async def get_quote(self, symbol: str) -> dict:
        data = await self._client.get_quote(symbol)
        # Normalize day_high/day_low → high/low to match TT MarketData shape
        return {
            "symbol":     data.get("symbol", symbol.upper()),
            "bid":        data.get("bid"),
            "ask":        data.get("ask"),
            "last":       data.get("last"),
            "mark":       data.get("mark"),
            "open":       data.get("open"),
            "high":       data.get("day_high"),
            "low":        data.get("day_low"),
            "close":      data.get("prev_close"),
            "volume":     data.get("volume"),
            "updated_at": None,
        }

    async def get_metrics(self, symbol: str) -> dict:
        items = await self._client.get_metrics([symbol])
        return items[0] if items else {"symbol": symbol}

    async def get_pb_ratio(self, symbol: str) -> float | None:
        def _fetch():
            val = self._client.get_info(symbol).get("priceToBook")
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None
        return await asyncio.to_thread(_fetch)

    async def get_bars(self, symbol: str, period: str, interval: str) -> list[dict]:
        yf_symbol = _YF_SYMBOL_MAP.get(symbol.upper(), symbol)
        yc_period = _YF_TO_YC.get(interval, interval)
        from_dt   = _from_date(period)
        candles   = await self._client.get_candles(
            symbols=[yf_symbol],
            period=yc_period,
            from_date=from_dt,
        )
        return _candles_to_bars(candles)
