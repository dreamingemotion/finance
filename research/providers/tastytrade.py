"""
Tastytrade market data provider (primary).
Thin wrapper around shared.data.brokers.tastytrade.TastytradeClient.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from shared.data.brokers.tastytrade import TastytradeClient

from .base import MarketDataProvider

# yfinance-style interval → DXLink candle period (only differences listed)
_YF_TO_TT: dict[str, str] = {
    "1wk": "1w",
}

_PERIOD_DAYS: dict[str, int | None] = {
    "1d":  1,   "3d":  3,   "5d":  5,   "1mo": 30,  "2mo": 60,
    "3mo": 90,  "6mo": 180, "1y":  365, "2y":  730, "3y":  1095,
    "5y":  1825, "10y": 3650, "max": None,
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
        client = self._get_client()
        results = await asyncio.gather(
            client.stream_quotes([symbol],     duration_seconds=2.0),
            client.stream_trades([symbol],     duration_seconds=2.0),
            client.stream_summaries([symbol],  duration_seconds=2.0),
            return_exceptions=True,
        )
        quotes, trades, summaries = results

        if all(isinstance(r, Exception) for r in results):
            raise results[0]

        q = quotes[0]     if not isinstance(quotes,    Exception) and quotes    else {}
        t = trades[0]     if not isinstance(trades,    Exception) and trades    else {}
        s = summaries[0]  if not isinstance(summaries, Exception) and summaries else {}

        bid  = q.get("bid_price")
        ask  = q.get("ask_price")
        last = t.get("price")

        mark = None
        if bid is not None and ask is not None:
            try:
                mark = round((float(bid) + float(ask)) / 2, 4)
            except (TypeError, ValueError):
                pass
        if mark is None:
            mark = last

        updated_at = None
        trade_time = t.get("time")
        if trade_time:
            try:
                updated_at = datetime.fromtimestamp(int(trade_time) / 1000, tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                pass

        close = s.get("day_close_price") or s.get("prev_day_close_price")

        def _f(v):
            return float(v) if v is not None else None

        return {
            "symbol":     symbol.upper(),
            "bid":        _f(bid),
            "ask":        _f(ask),
            "last":       _f(last),
            "mark":       _f(mark),
            "open":       _f(s.get("day_open_price")),
            "high":       _f(s.get("day_high_price")),
            "low":        _f(s.get("day_low_price")),
            "close":      _f(close),
            "volume":     t.get("day_volume"),
            "updated_at": updated_at,
        }

    async def get_quotes_batch(self, symbols: list[str]) -> dict[str, dict]:
        """
        Stream quotes, trades, and summaries for all symbols using 3 DXLink
        connections total (instead of 3 × N when calling get_quote per symbol).
        Returns a dict keyed by uppercase symbol.
        """
        client = self._get_client()
        results = await asyncio.gather(
            client.stream_quotes(symbols,    duration_seconds=2.0),
            client.stream_trades(symbols,    duration_seconds=2.0),
            client.stream_summaries(symbols, duration_seconds=2.0),
            return_exceptions=True,
        )
        quotes_list, trades_list, summaries_list = results

        if all(isinstance(r, Exception) for r in results):
            raise results[0]

        def _by_sym(lst: list | Exception) -> dict:
            if isinstance(lst, Exception):
                return {}
            return {e.get("event_symbol", "").upper(): e for e in lst}

        q_map = _by_sym(quotes_list)
        t_map = _by_sym(trades_list)
        s_map = _by_sym(summaries_list)

        def _f(v):
            return float(v) if v is not None else None

        out: dict[str, dict] = {}
        for sym in symbols:
            sym_u = sym.upper()
            q = q_map.get(sym_u, {})
            t = t_map.get(sym_u, {})
            s = s_map.get(sym_u, {})

            bid  = q.get("bid_price")
            ask  = q.get("ask_price")
            last = t.get("price")

            mark: float | None = None
            if bid is not None and ask is not None:
                try:
                    mark = round((float(bid) + float(ask)) / 2, 4)
                except (TypeError, ValueError):
                    pass
            if mark is None:
                mark = _f(last)

            close = s.get("day_close_price") or s.get("prev_day_close_price")
            out[sym_u] = {
                "symbol": sym_u,
                "bid":    _f(bid),
                "ask":    _f(ask),
                "last":   _f(last),
                "mark":   mark,
                "close":  _f(close),
            }

        return out

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
            duration_seconds=5.0,
        )
        return _candles_to_bars(candles)
