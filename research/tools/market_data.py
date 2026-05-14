"""
Market data MCP tool implementations.

Three tools:
  get_quote(symbol)               — real-time price, bid/ask/mark, day OHLCV
  get_snapshot(symbol)            — quote + full metrics (P/E, P/B, IV, beta…)
  get_bars(symbol, period, interval) — OHLCV history for charting

Primary source: tastytrade (REST + DXLink streaming)
Secondary/fallback: yfinance

P/B ratio is always sourced from yfinance (tastytrade metrics don't include it).
Every response includes data_source ("primary" | "secondary") and stale flags.
"""
from __future__ import annotations

import asyncio
import logging

from research.providers.base import Bar, get_last_market_close, is_quote_stale, is_stale
from research.providers.tastytrade import TastytradeProvider
from research.providers.yfinance_provider import YFinanceProvider

logger = logging.getLogger("finance-research")

_tt = TastytradeProvider()
_yf = YFinanceProvider()


# ---------------------------------------------------------------------------
# get_quote
# ---------------------------------------------------------------------------

async def get_quote(symbol: str) -> dict:
    """
    Fetch a real-time quote for a single equity symbol.

    Returns price, bid, ask, mark, day open/high/low/close, volume,
    data_source, and stale flag.
    """
    try:
        data = await _tt.get_quote(symbol)
        data_source = "primary"
    except Exception as exc:
        logger.warning("tastytrade get_quote failed for %s: %s — falling back to yfinance", symbol, exc)
        data = await _yf.get_quote(symbol)
        data_source = "secondary"

    updated_at_str = data.pop("updated_at", None)
    updated_at = None
    if updated_at_str:
        from datetime import datetime
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
        except ValueError:
            pass

    return {
        **data,
        "data_source": data_source,
        "stale":       is_quote_stale(updated_at),
    }


# ---------------------------------------------------------------------------
# get_snapshot
# ---------------------------------------------------------------------------

async def get_snapshot(symbol: str) -> dict:
    """
    Fetch a full market snapshot: quote + metrics for a single equity symbol.

    Includes price, bid/ask/mark, P/E (tastytrade), P/B (yfinance),
    IV rank, HV, beta, market cap, dividend yield, and borrow rate.
    data_source reflects whether tastytrade (primary) or yfinance
    (secondary) provided the quote and metrics data.
    P/B is always sourced from yfinance regardless of the primary result.
    """
    # P/B always comes from yfinance; fetch in parallel with tastytrade data
    results = await asyncio.gather(
        _tt.get_quote(symbol),
        _tt.get_metrics(symbol),
        _yf.get_pb_ratio(symbol),
        return_exceptions=True,
    )
    tt_quote, tt_metrics, pb_ratio = results

    tt_ok = not isinstance(tt_quote, Exception) and not isinstance(tt_metrics, Exception)

    if tt_ok:
        if isinstance(pb_ratio, Exception):
            pb_ratio = None
        updated_at_str = tt_quote.pop("updated_at", None)
        data_source = "primary"
        merged = {**tt_quote, **tt_metrics}
    else:
        exc = tt_quote if isinstance(tt_quote, Exception) else tt_metrics
        logger.warning("tastytrade snapshot failed for %s: %s — falling back to yfinance", symbol, exc)
        yf_quote, yf_metrics = await asyncio.gather(
            _yf.get_quote(symbol),
            _yf.get_metrics(symbol),
        )
        updated_at_str = yf_quote.pop("updated_at", None)
        # pb_ratio was already fetched in the initial gather; reuse it if valid
        if isinstance(pb_ratio, Exception):
            pb_ratio = None
        data_source = "secondary"
        merged = {**yf_quote, **yf_metrics}

    updated_at = None
    if updated_at_str:
        from datetime import datetime
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
        except ValueError:
            pass

    merged.pop("updated_at", None)
    merged["pb_ratio"] = pb_ratio
    merged["data_source"] = data_source
    merged["stale"] = is_quote_stale(updated_at)
    return merged


# ---------------------------------------------------------------------------
# get_bars
# ---------------------------------------------------------------------------

async def get_full_timeframe(symbol: str) -> dict:
    """
    Fetch four standard timeframes for full continuity analysis.

    Runs all four get_bars calls in parallel.
    """
    labels = [
        ("3-Year Monthly",   "3y",  "1mo"),
        ("2-Year Weekly",    "2y",  "1wk"),
        ("2-Month Daily",    "2mo", "1d"),
        ("3-Day Hourly",     "3d",  "1h"),
    ]
    results = await asyncio.gather(
        *[get_bars(symbol, p, i) for _, p, i in labels],
        return_exceptions=True,
    )
    charts = []
    for (label, period, interval), result in zip(labels, results):
        if isinstance(result, Exception):
            charts.append({"label": label, "period": period, "interval": interval, "error": str(result)})
        else:
            charts.append({"label": label, **result})
    return {"symbol": symbol.upper(), "charts": charts}


async def get_bars(symbol: str, period: str, interval: str) -> dict:
    """
    Fetch OHLCV bars for charting.

    period:    1d 5d 1mo 3mo 6mo 1y 2y 5y  (look-back window)
    interval:  1m 5m 15m 30m 1h 1d 1wk 1mo (bar width)

    Returns a bars list plus metadata: symbol, period, interval,
    bar_count, data_source, and last_bar_stale.

    Notes:
    - tastytrade delivers bars via DXLink WebSocket streaming; a 3-second
      idle timeout signals the end of the historical burst.
    - yfinance is used as fallback if tastytrade returns no data or errors.
    """
    bars: list[dict] = []
    data_source = "primary"

    try:
        bars = await _tt.get_bars(symbol, period, interval)
        if not bars:
            raise ValueError("tastytrade returned zero bars")
    except Exception as exc:
        logger.warning("tastytrade get_bars failed for %s: %s — falling back to yfinance", symbol, exc)
        bars = await _yf.get_bars(symbol, period, interval)
        data_source = "secondary"

    last_stale = False
    if bars:
        from datetime import datetime, timezone
        last_time_str = bars[-1]["time"]
        try:
            last_dt = datetime.fromisoformat(last_time_str)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            last_bar = Bar(
                time=last_dt,
                open=bars[-1]["open"],
                high=bars[-1]["high"],
                low=bars[-1]["low"],
                close=bars[-1]["close"],
                volume=bars[-1].get("volume"),
            )
            last_stale = is_stale(last_bar)
        except Exception:
            pass

    return {
        "symbol":         symbol,
        "period":         period,
        "interval":       interval,
        "bar_count":      len(bars),
        "data_source":    data_source,
        "last_bar_stale": last_stale,
        "bars":           bars,
    }
