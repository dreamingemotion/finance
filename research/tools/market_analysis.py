"""
Market analysis tool — daily overview of major indexes, S&P 500 sectors,
Treasury yields, and VIX, enriched with knowledge base insights.

All data sourced from market_data.get_bars (tastytrade primary, yfinance fallback).
Knowledge context sourced from the shared knowledge base.
No web searches are performed.
"""
from __future__ import annotations

import asyncio

from research.tools.fred import get_treasury_yields
from research.tools.knowledge import search_knowledge
from research.tools.market_data import _CHART_STYLE, get_bars

# ---------------------------------------------------------------------------
# Symbol definitions
# ---------------------------------------------------------------------------

_INDEX_DEFS = [
    {"symbol": "SPX", "label": "S&P 500",     "grid_position": "top-left"},
    {"symbol": "DJX", "label": "Dow Jones",   "grid_position": "top-right"},
    {"symbol": "NDX", "label": "Nasdaq 100",  "grid_position": "bottom-left"},
    {"symbol": "IWM", "label": "Russell 2000", "grid_position": "bottom-right"},
]

_SECTOR_DEFS = [
    {"symbol": "XLK",  "label": "Technology"},
    {"symbol": "XLF",  "label": "Financials"},
    {"symbol": "XLV",  "label": "Health Care"},
    {"symbol": "XLE",  "label": "Energy"},
    {"symbol": "XLI",  "label": "Industrials"},
    {"symbol": "XLP",  "label": "Consumer Staples"},
    {"symbol": "XLY",  "label": "Consumer Discretionary"},
    {"symbol": "XLB",  "label": "Materials"},
    {"symbol": "XLRE", "label": "Real Estate"},
    {"symbol": "XLU",  "label": "Utilities"},
    {"symbol": "XLC",  "label": "Communication Services"},
]


_KNOWLEDGE_QUERIES = [
    ("sector",     "equity market sector rotation breadth risk sentiment",          ["macro", "sector", "strategy"]),
    ("yields",     "treasury yield curve interest rates economic outlook",           ["macro", "market_risk"]),
    ("volatility", "VIX volatility market fear greed risk-off",                     ["market_risk", "sentiment"]),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _day_change_pct(bars: list[dict]) -> float | None:
    if len(bars) < 2:
        return None
    prev = bars[-2]["close"]
    curr = bars[-1]["close"]
    if not prev:
        return None
    return round((curr - prev) / prev * 100, 2)


def _split_today_bars(bars: list[dict]) -> tuple[list[dict], float | None]:
    """Return (today_bars, prev_session_close) by splitting on the most-recent date."""
    from datetime import datetime
    if not bars:
        return [], None
    dates = []
    for b in bars:
        try:
            dates.append(datetime.fromisoformat(b["time"]).date())
        except Exception:
            dates.append(None)
    valid_dates = [d for d in dates if d is not None]
    if not valid_dates:
        return bars, None
    latest = max(valid_dates)
    today_bars = [b for b, d in zip(bars, dates) if d == latest]
    prev_bars  = [b for b, d in zip(bars, dates) if d is not None and d < latest]
    prev_close = prev_bars[-1]["close"] if prev_bars else None
    return today_bars, prev_close


def _slim_bars(bars: list[dict]) -> list[dict]:
    """Strip volume and round OHLCV values to reduce response payload size."""
    return [
        {
            "time":  b["time"],
            "open":  round(b["open"],  2),
            "high":  round(b["high"],  2),
            "low":   round(b["low"],   2),
            "close": round(b["close"], 2),
        }
        for b in bars
    ]


def _yield_curve_spreads(yields: list[dict]) -> dict:
    """Compute spread metrics so the LLM can characterise curve shape."""
    by_mat = {y["maturity"]: y.get("yield_pct") for y in yields}

    def spread(long: str, short: str) -> float | None:
        vl, vs = by_mat.get(long), by_mat.get(short)
        if vl is None or vs is None:
            return None
        return round(vl - vs, 3)

    s_2_10  = spread("10Y", "2Y")
    s_3m_10 = spread("10Y", "3M")
    s_10_30 = spread("30Y", "10Y")

    shape_notes: list[str] = []
    if s_2_10 is not None:
        if s_2_10 < 0:
            shape_notes.append("2Y-10Y inverted (short end higher than long end)")
        elif s_2_10 < 0.25:
            shape_notes.append("2Y-10Y nearly flat")
        else:
            shape_notes.append("2Y-10Y normal (positive slope)")
    if s_3m_10 is not None:
        if s_3m_10 < 0:
            shape_notes.append("3M-10Y inverted")
        elif s_3m_10 < 0.25:
            shape_notes.append("3M-10Y nearly flat")

    return {
        "spread_2y_10y":  s_2_10,
        "spread_3m_10y":  s_3m_10,
        "spread_10y_30y": s_10_30,
        "shape_notes":    shape_notes,
    }


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------

async def get_market_analysis() -> dict:
    """
    Fetch daily market analysis data for major indexes, all 11 S&P 500 sectors,
    Treasury yields, VIX, and knowledge base insights — all in one parallel fetch.

    Returns five top-level sections:
      index_charts       — 1-year daily OHLCV bars for SPX, DJX, NDX, RUT
      sector_performance — day-over-day % change for all 11 GICS sectors, sorted desc
      treasury_yields    — yield levels and bps changes for 3M/2Y/5Y/10Y/30Y
                           plus curve_shape spread metrics
      vix                — VIX level, previous level, and day % change (no chart)
      knowledge          — relevant knowledge base insights for sectors, yields,
                           and volatility to inform LLM opinion
    """
    n_idx = len(_INDEX_DEFS)
    n_sec = len(_SECTOR_DEFS)

    results = await asyncio.gather(
        *[get_bars(d["symbol"], period="2d", interval="5m") for d in _INDEX_DEFS],
        *[get_bars(d["symbol"], period="5d", interval="1d") for d in _SECTOR_DEFS],
        get_treasury_yields(),
        get_bars("VIX", period="5d", interval="1d"),
        *[search_knowledge(q, categories=cats, limit=3) for _, q, cats in _KNOWLEDGE_QUERIES],
        return_exceptions=True,
    )

    idx_results = results[:n_idx]
    sec_results = results[n_idx : n_idx + n_sec]
    tsy_result  = results[n_idx + n_sec]
    vix_result  = results[n_idx + n_sec + 1]
    kn_results  = results[n_idx + n_sec + 2 :]

    # --- Index charts (2×2 grid) --------------------------------------------
    charts: list[dict] = []
    for render_order, (defn, result) in enumerate(zip(_INDEX_DEFS, idx_results)):
        entry: dict = {
            "render_order":       render_order,
            "grid_position":      defn["grid_position"],
            "label":              defn["label"],
            "symbol":             defn["symbol"],
            "period":             "1d",
            "interval":           "5m",
            "suppress_time_gaps": True,
            "chart_style":        _CHART_STYLE,
        }
        if isinstance(result, Exception):
            entry["error"]          = str(result)
            entry["day_change_pct"] = None
            entry["bars"]           = []
            entry["bar_count"]      = 0
        else:
            all_bars = result.get("bars", [])
            today_bars, prev_close = _split_today_bars(all_bars)
            slimmed = _slim_bars(today_bars)
            entry["bars"]           = slimmed
            entry["bar_count"]      = len(slimmed)
            entry["data_source"]    = result.get("data_source")
            entry["last_bar_stale"] = result.get("last_bar_stale")
            # Day change from first intraday bar's open to last bar's close
            first_open = today_bars[0]["open"] if today_bars else None
            last_close = today_bars[-1]["close"] if today_bars else None
            entry["day_change_pct"] = (
                round((last_close - first_open) / first_open * 100, 2)
                if first_open and last_close else None
            )
            entry["prev_close"] = round(prev_close, 2) if prev_close else None
        charts.append(entry)

    # --- Sector performance --------------------------------------------------
    sectors: list[dict] = []
    for defn, result in zip(_SECTOR_DEFS, sec_results):
        entry = {"symbol": defn["symbol"], "label": defn["label"]}
        if isinstance(result, Exception):
            entry["error"]          = str(result)
            entry["day_change_pct"] = None
        else:
            bars = result.get("bars", [])
            entry["day_change_pct"] = _day_change_pct(bars)
            entry["data_source"]    = result.get("data_source")
            if bars:
                entry["current_price"] = bars[-1]["close"]
                entry["prev_close"]    = bars[-2]["close"] if len(bars) >= 2 else None
        sectors.append(entry)

    sectors.sort(key=lambda x: x.get("day_change_pct") or 0.0, reverse=True)

    # --- Treasury yields (FRED) ---------------------------------------------
    if isinstance(tsy_result, Exception):
        tsy_yields: list[dict] = []
        tsy_as_of: str | None = None
    else:
        tsy_yields = tsy_result.get("yields", [])
        tsy_as_of  = tsy_result.get("as_of")

    # --- VIX ----------------------------------------------------------------
    if isinstance(vix_result, Exception):
        vix = {"symbol": "VIX", "error": str(vix_result), "current_level": None,
               "prev_level": None, "day_change_pct": None}
    else:
        bars = vix_result.get("bars", [])
        curr = bars[-1]["close"] if bars else None
        prev = bars[-2]["close"] if len(bars) >= 2 else None
        vix = {
            "symbol":        "VIX",
            "current_level": curr,
            "prev_level":    prev,
            "day_change_pct": _day_change_pct(bars),
            "data_source":   vix_result.get("data_source"),
        }

    # --- Knowledge ----------------------------------------------------------
    knowledge: dict = {}
    for (key, _, _), result in zip(_KNOWLEDGE_QUERIES, kn_results):
        knowledge[key] = result if not isinstance(result, Exception) else {"error": str(result)}

    return {
        "analysis_type": "market_analysis",
        "index_charts": {
            "layout":   "2x2",
            "period":   "1d",
            "interval": "5m",
            "charts":   charts,
        },
        "sector_performance": {
            "render_as": "horizontal_bar_chart",
            "sort":      "day_change_pct_desc",
            "sectors":   sectors,
        },
        "vix": vix,
        "treasury_yields": {
            "render_as":   "table_and_yield_curve",
            "data_source": "fred",
            "as_of":       tsy_as_of,
            "maturities":  ["3M", "2Y", "5Y", "10Y", "30Y"],
            "yields":      tsy_yields,
            "curve_shape": _yield_curve_spreads(tsy_yields),
        },
        "knowledge": knowledge,
    }
