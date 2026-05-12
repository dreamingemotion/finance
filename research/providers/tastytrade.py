"""
Tastytrade market data provider (primary).

REST for snapshot quotes and metrics; DXLink WebSocket streaming for
OHLCV bars.  Historical bars are fetched by subscribing with a fromTime
in the past — DXLink delivers the full history then continues live.
A per-event timeout stops the generator once the historical burst ends.

Environment variables required:
  TASTYTRADE_CLIENT_ID
  TASTYTRADE_CLIENT_SECRET
  TASTYTRADE_REFRESH_TOKEN
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from tastytrade_data import CandlePeriod, DXLinkStreamer, TastytradeAuth, TastytradeClient
from tastytrade_data.market_data import get_market_data
from tastytrade_data.metrics import get_market_metrics

from .base import MarketDataProvider

# yfinance-style interval → DXLink CandlePeriod string
_INTERVAL_MAP: dict[str, str] = {
    "1m":  CandlePeriod.ONE_MINUTE,
    "2m":  CandlePeriod.TWO_MINUTE,
    "5m":  CandlePeriod.FIVE_MINUTE,
    "15m": CandlePeriod.FIFTEEN_MINUTE,
    "30m": CandlePeriod.THIRTY_MINUTE,
    "60m": CandlePeriod.ONE_HOUR,
    "1h":  CandlePeriod.ONE_HOUR,
    "1d":  CandlePeriod.ONE_DAY,
    "1wk": CandlePeriod.ONE_WEEK,
    "1mo": CandlePeriod.ONE_MONTH,
}

# period → look-back in calendar days (None = full history)
_PERIOD_DAYS: dict[str, int | None] = {
    "1d":  1,
    "5d":  5,
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y":  365,
    "2y":  730,
    "5y":  1825,
    "10y": 3650,
    "max": None,
}

# seconds to wait for the next candle before assuming the historical burst is done
_CANDLE_TIMEOUT = 3.0


def _make_auth() -> TastytradeAuth:
    return TastytradeAuth(
        client_id=os.environ["TASTYTRADE_CLIENT_ID"],
        client_secret=os.environ["TASTYTRADE_CLIENT_SECRET"],
        refresh_token=os.environ["TASTYTRADE_REFRESH_TOKEN"],
    )


def _from_ms(period: str) -> int:
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return int(1e9)  # ~2001-09-09 — maximum available history
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return int(dt.timestamp() * 1000)


def _dec(v) -> float | None:
    return float(v) if v is not None else None


class TastytradeProvider(MarketDataProvider):

    async def get_quote(self, symbol: str) -> dict:
        auth = _make_auth()
        async with TastytradeClient(auth) as client:
            md = await get_market_data(client, symbol, "equity")
        return {
            "symbol":     symbol,
            "bid":        _dec(md.bid),
            "ask":        _dec(md.ask),
            "last":       _dec(md.last),
            "mark":       _dec(md.mark),
            "open":       _dec(md.open),
            "high":       _dec(md.high),
            "low":        _dec(md.low),
            "close":      _dec(md.close),
            "volume":     md.volume,
            "updated_at": md.updated_at.isoformat() if md.updated_at else None,
        }

    async def get_metrics(self, symbol: str) -> dict:
        auth = _make_auth()
        async with TastytradeClient(auth) as client:
            items = await get_market_metrics(client, [symbol])
        if not items:
            return {"symbol": symbol}
        m = items[0]
        return {
            "symbol":          symbol,
            "pe_ratio":        _dec(m.price_earnings_ratio),
            "eps":             _dec(m.earnings_per_share),
            "market_cap":      _dec(m.market_cap),
            "iv_rank":         _dec(m.implied_volatility_rank),
            "iv_index":        _dec(m.implied_volatility_index),
            "hv_30d":          _dec(m.historical_volatility_30_day),
            "hv_60d":          _dec(m.historical_volatility_60_day),
            "beta":            _dec(m.beta),
            "dividend_yield":  _dec(m.dividend_yield),
            "borrow_rate":     _dec(m.borrow_rate),
        }

    async def get_bars(self, symbol: str, period: str, interval: str) -> list[dict]:
        tt_period = _INTERVAL_MAP.get(interval, CandlePeriod.ONE_DAY)
        from_ms   = _from_ms(period)
        bars: list[dict] = []

        auth = _make_auth()
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for candle in streamer.stream_candles(
                    symbols=[symbol],
                    period=tt_period,
                    from_time_ms=from_ms,
                    timeout=_CANDLE_TIMEOUT,
                ):
                    bars.append({
                        "time":   datetime.fromtimestamp(
                            candle.time / 1000, tz=timezone.utc
                        ).isoformat(),
                        "open":   float(candle.open),
                        "high":   float(candle.high),
                        "low":    float(candle.low),
                        "close":  float(candle.close),
                        "volume": float(candle.volume) if candle.volume is not None else None,
                    })

        bars.sort(key=lambda b: b["time"])
        return bars
