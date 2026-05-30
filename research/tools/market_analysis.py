"""
Market analysis tool — daily overview of major indexes, S&P 500 sectors, and Treasury yields.

All data sourced from market_data.get_bars (tastytrade primary, yfinance fallback).
No web searches are performed.
"""
from __future__ import annotations

import asyncio

from research.tools.market_data import _CHART_STYLE, get_bars

# ---------------------------------------------------------------------------
# Symbol definitions
# ---------------------------------------------------------------------------

_INDEX_DEFS = [
    {"symbol": "SPX", "label": "S&P 500",     "grid_position": "top-left"},
    {"symbol": "DJX", "label": "Dow Jones",   "grid_position": "top-right"},
    {"symbol": "NDX", "label": "Nasdaq 100",  "grid_position": "bottom-left"},
    {"symbol": "RUT", "label": "Russell 2000", "grid_position": "bottom-right"},
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

# ^IRX=3M  ^TWOYEAR=2Y  ^FVX=5Y  ^TNX=10Y  ^TYX=30Y
# Yield values are returned in percentage points (e.g. 4.35 = 4.35%).
# ^TWOYEAR may not be available in all yfinance versions; errors are handled gracefully.
_TREASURY_DEFS = [
    {"symbol": "^IRX",     "label": "3-Month", "maturity": "3M"},
    {"symbol": "^TWOYEAR", "label": "2-Year",  "maturity": "2Y"},
    {"symbol": "^FVX",     "label": "5-Year",  "maturity": "5Y"},
    {"symbol": "^TNX",     "label": "10-Year", "maturity": "10Y"},
    {"symbol": "^TYX",     "label": "30-Year", "maturity": "30Y"},
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
    and the 5-point Treasury yield curve.

    All requests run in parallel. Returns three top-level sections:
      index_charts      — 1-year daily OHLCV bars for SPX, DJIA, Nasdaq, Russell 2000
      sector_performance — day-over-day % change for all 11 GICS sectors, sorted desc
      treasury_yields   — yield levels and basis-point changes for 3M/2Y/5Y/10Y/30Y,
                          plus curve_shape spread metrics
    """
    n_idx = len(_INDEX_DEFS)
    n_sec = len(_SECTOR_DEFS)

    results = await asyncio.gather(
        *[get_bars(d["symbol"], period="1y", interval="1d") for d in _INDEX_DEFS],
        *[get_bars(d["symbol"], period="5d", interval="1d") for d in _SECTOR_DEFS],
        *[get_bars(d["symbol"], period="5d", interval="1d") for d in _TREASURY_DEFS],
        return_exceptions=True,
    )

    idx_results = results[:n_idx]
    sec_results = results[n_idx : n_idx + n_sec]
    tsy_results = results[n_idx + n_sec :]

    # --- Index charts (2×2 grid) --------------------------------------------
    charts: list[dict] = []
    for render_order, (defn, result) in enumerate(zip(_INDEX_DEFS, idx_results)):
        entry: dict = {
            "render_order":      render_order,
            "grid_position":     defn["grid_position"],
            "label":             defn["label"],
            "symbol":            defn["symbol"],
            "period":            "1y",
            "interval":          "1d",
            "suppress_time_gaps": False,
            "chart_style":       _CHART_STYLE,
        }
        if isinstance(result, Exception):
            entry["error"]          = str(result)
            entry["day_change_pct"] = None
            entry["bars"]           = []
            entry["bar_count"]      = 0
        else:
            bars = result.get("bars", [])
            entry["bars"]           = bars
            entry["bar_count"]      = result.get("bar_count", len(bars))
            entry["data_source"]    = result.get("data_source")
            entry["last_bar_stale"] = result.get("last_bar_stale")
            entry["day_change_pct"] = _day_change_pct(bars)
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

    # --- Treasury yields ----------------------------------------------------
    yields: list[dict] = []
    for defn, result in zip(_TREASURY_DEFS, tsy_results):
        entry = {
            "maturity": defn["maturity"],
            "label":    defn["label"],
            "symbol":   defn["symbol"],
        }
        if isinstance(result, Exception):
            entry["error"]          = str(result)
            entry["yield_pct"]      = None
            entry["prev_yield_pct"] = None
            entry["change_bps"]     = None
        else:
            bars = result.get("bars", [])
            curr_yield = bars[-1]["close"] if bars else None
            prev_yield = bars[-2]["close"] if len(bars) >= 2 else None
            entry["yield_pct"]      = curr_yield
            entry["prev_yield_pct"] = prev_yield
            entry["change_bps"] = (
                round((curr_yield - prev_yield) * 100, 1)
                if curr_yield is not None and prev_yield is not None
                else None
            )
            entry["data_source"] = result.get("data_source")
        yields.append(entry)

    return {
        "analysis_type": "market_analysis",
        "index_charts": {
            "layout":   "2x2",
            "period":   "1y",
            "interval": "1d",
            "charts":   charts,
        },
        "sector_performance": {
            "render_as": "horizontal_bar_chart",
            "sort":      "day_change_pct_desc",
            "sectors":   sectors,
        },
        "treasury_yields": {
            "render_as":  "table_and_yield_curve",
            "maturities": ["3M", "2Y", "5Y", "10Y", "30Y"],
            "yields":     yields,
            "curve_shape": _yield_curve_spreads(yields),
        },
    }
