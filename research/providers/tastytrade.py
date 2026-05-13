"""
Tastytrade market data provider (primary).
Thin wrapper around shared.data.brokers.tastytrade.TastytradeClient.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from shared.data.brokers.tastytrade import TastytradeClient

from .base import MarketDataProvider

# yfinance-style interval → DXLink candle period (only differences listed)
_YF_TO_TT: dict[str, str] = {
    "1wk": "1w",
}

_PERIOD_DAYS: dict[str, int | None] = {
    "1d":  1,   "5d":  5,   "1mo": 30,  "3mo": 90,
    "6mo": 180, "1y":  365, "2y":  730, "5y":  1825,
    "10y": 3650, "max": None,
}


def _from_date(period: str) -> str | None:
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None  # full history — TastytradeClient treats None as full history
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


class TastytradeProvider(MarketDataProvider):

    def __init__(self) -> None:
        self._client: TastytradeClient | None = None

    def _get_client(self) -> TastytradeClient:
        if self._client is None:
            self._client = TastytradeClient(
                client_id=os.environ["TT_CLIENT_ID"],
                client_secret=os.environ["TT_CLIENT_SECRET"],
                refresh_token=os.environ["TT_REFRESH_TOKEN"],
            )
        return self._client

    async def get_quote(self, symbol: str) -> dict:
        items = await self._get_client().get_quotes(equities=[symbol])
        return items[0] if items else {"symbol": symbol}

    async def get_metrics(self, symbol: str) -> dict:
        items = await self._get_client().get_metrics([symbol])
        return items[0] if items else {"symbol": symbol}

    async def get_bars(self, symbol: str, period: str, interval: str) -> list[dict]:
        tt_period = _YF_TO_TT.get(interval, interval)
        from_dt   = _from_date(period)
        candles   = await self._get_client().stream_candles(
            symbols=[symbol],
            period=tt_period,
            from_date=from_dt,
            duration_seconds=10.0,
        )
        return _candles_to_bars(candles)
