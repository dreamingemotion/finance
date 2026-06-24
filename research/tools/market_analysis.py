"""
Market analysis tool — daily overview of major indexes, S&P 500 sectors,
Treasury yields, and VIX, enriched with knowledge base insights.

All data sourced from market_data.get_bars (tastytrade primary, yfinance fallback).
Knowledge context sourced from the shared knowledge base.
No web searches are performed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from research.tools.fred import get_treasury_yields
from research.tools.knowledge import search_knowledge
from research.tools.market_data import _CHART_STYLE, get_bars, get_quotes_batch

# ---------------------------------------------------------------------------
# Symbol definitions
# ---------------------------------------------------------------------------

_INDEX_DEFS = [
    {"symbol": "SPX", "label": "S&P 500",     "grid_position": "top-left"},
    {"symbol": "DJX", "label": "Dow Jones",   "grid_position": "top-right", "price_multiplier": 100},
    {"symbol": "IXIC", "label": "Nasdaq Composite", "grid_position": "bottom-left"},
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

_UP   = _CHART_STYLE["up_color"]    # #1d9e75
_DOWN = _CHART_STYLE["down_color"]  # #d85a30


def _is_market_open() -> bool:
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    open_time  = et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_time = et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_time <= et < close_time


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _pct_color(pct: float | None) -> str:
    return _UP if (pct or 0) >= 0 else _DOWN


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "N/A"
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"


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

    curve_color = _UP if (s_3m_10 is not None and s_3m_10 >= 0) else _DOWN

    return {
        "spread_2y_10y":  s_2_10,
        "spread_3m_10y":  s_3m_10,
        "spread_10y_30y": s_10_30,
        "shape_notes":    shape_notes,
        "curve_color":    curve_color,
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
    _sec_syms = [d["symbol"] for d in _SECTOR_DEFS]

    results = await asyncio.gather(
        *[get_bars(d["symbol"], period="2d", interval="5m") for d in _INDEX_DEFS],
        get_bars("VIX", period="2d", interval="5m"),
        get_quotes_batch(_sec_syms),
        get_treasury_yields(),
        *[search_knowledge(q, categories=cats, limit=3) for _, q, cats in _KNOWLEDGE_QUERIES],
        return_exceptions=True,
    )

    idx_results  = results[:n_idx]
    vix_bars     = results[n_idx]
    batch_result = results[n_idx + 1]
    tsy_result   = results[n_idx + 2]
    kn_results   = results[n_idx + 3:]

    batch: dict[str, dict] = {} if isinstance(batch_result, Exception) else batch_result

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
            multiplier = defn.get("price_multiplier", 1)
            if multiplier != 1:
                today_bars = [
                    {**b,
                     "open":  b["open"]  * multiplier,
                     "high":  b["high"]  * multiplier,
                     "low":   b["low"]   * multiplier,
                     "close": b["close"] * multiplier}
                    for b in today_bars
                ]
                prev_close = prev_close * multiplier if prev_close is not None else None
            slimmed = _slim_bars(today_bars)
            entry["bars"]           = slimmed
            entry["bar_count"]      = len(slimmed)
            entry["data_source"]    = result.get("data_source")
            entry["last_bar_stale"] = result.get("last_bar_stale")
            # Day change: prev session close → current last close (standard convention)
            last_close = today_bars[-1]["close"] if today_bars else None
            entry["day_change_pct"] = (
                round((last_close - prev_close) / prev_close * 100, 2)
                if prev_close and last_close else None
            )
            pc = round(prev_close, 2) if prev_close else None
            entry["prev_close"]    = pc
            entry["pct_color"]     = _pct_color(entry["day_change_pct"])
            entry["formatted_pct"] = _fmt_pct(entry["day_change_pct"])
            # Expand price range to include prev_close so the line is always on-screen
            if slimmed:
                lows  = [b["low"]  for b in slimmed]
                highs = [b["high"] for b in slimmed]
                if pc is not None:
                    lows.append(pc)
                    highs.append(pc)
                pad = (max(highs) - min(lows)) * 0.03 or min(lows) * 0.01
                entry["price_min"] = round(min(lows)  - pad, 2)
                entry["price_max"] = round(max(highs) + pad, 2)
            entry["overlays"] = (
                [{"type": "hline", "price": pc,
                  "color": "rgba(255,255,255,0.5)", "dash": [4, 4]}]
                if pc is not None else []
            )
        charts.append(entry)

    # --- Sector performance --------------------------------------------------
    sectors: list[dict] = []
    for defn in _SECTOR_DEFS:
        sym    = defn["symbol"]
        result = batch.get(sym.upper())
        entry  = {"symbol": sym, "label": defn["label"]}
        if result is None or "error" in result:
            entry["error"]          = (result or {}).get("error", "no data")
            entry["day_change_pct"] = None
        else:
            current    = result.get("mark") or result.get("last")
            prev_close = result.get("close")
            pct = (
                round((current - prev_close) / prev_close * 100, 2)
                if prev_close and current else None
            )
            entry["day_change_pct"] = pct
            entry["data_source"]    = result.get("data_source")
            if current is not None:
                entry["current_price"] = current
                entry["prev_close"]    = prev_close
        entry["bar_color"]     = _pct_color(entry.get("day_change_pct"))
        entry["formatted_pct"] = _fmt_pct(entry.get("day_change_pct"))
        sectors.append(entry)

    # Fallback: fetch daily bars for sectors where get_quote returned no prev_close
    missing = [
        (i, defn) for i, (defn, entry) in enumerate(zip(_SECTOR_DEFS, sectors))
        if entry.get("prev_close") is None and entry.get("current_price") is not None
    ]
    if missing:
        fb_results = await asyncio.gather(
            *[get_bars(defn["symbol"], period="5d", interval="1d") for _, defn in missing],
            return_exceptions=True,
        )
        for (idx, _), fb in zip(missing, fb_results):
            if not isinstance(fb, Exception):
                bars = fb.get("bars", [])
                if len(bars) >= 2:
                    pc   = bars[-2]["close"]
                    cur  = sectors[idx]["current_price"]
                    pct  = round((cur - pc) / pc * 100, 2) if pc else None
                    sectors[idx]["prev_close"]    = round(pc, 2)
                    sectors[idx]["day_change_pct"] = pct
                    sectors[idx]["bar_color"]      = _pct_color(pct)
                    sectors[idx]["formatted_pct"]  = _fmt_pct(pct)

    sectors.sort(key=lambda x: x.get("day_change_pct") or 0.0, reverse=True)

    # --- Treasury yields (FRED) ---------------------------------------------
    if isinstance(tsy_result, Exception):
        tsy_yields: list[dict] = []
        tsy_as_of: str | None = None
    else:
        tsy_yields = tsy_result.get("yields", [])
        tsy_as_of  = tsy_result.get("as_of")
        for y in tsy_yields:
            bps = y.get("change_bps")
            yld = y.get("yield_pct")
            y["change_color"]    = (_UP if (bps or 0) > 0 else _DOWN if (bps or 0) < 0 else "#888888")
            y["formatted_yield"] = f"{yld:.3f}%" if yld is not None else "N/A"
            y["formatted_change"] = (
                f"+{bps:.1f} bps" if bps is not None and bps > 0
                else f"{bps:.1f} bps" if bps is not None
                else "N/A"
            )

    # --- VIX ----------------------------------------------------------------
    # VIX is a calculated index — stream_quotes/trades emit nothing for it, only
    # stream_summaries returns data. Fetch intraday bars instead, same as the
    # main indexes, so we get a reliable current level and yesterday's close.
    if isinstance(vix_bars, Exception) or not isinstance(vix_bars, dict):
        vix = {"symbol": "VIX", "error": str(vix_bars) if isinstance(vix_bars, Exception) else "no data",
               "current_level": None, "prev_level": None, "day_change_pct": None,
               "regime": None, "pct_color": None, "formatted_pct": "N/A"}
    else:
        _vix_all = vix_bars.get("bars", [])
        _vix_today, _vix_prev_close = _split_today_bars(_vix_all)
        curr = _vix_today[-1]["close"] if _vix_today else None
        prev = _vix_prev_close
        vix_pct = (
            round((curr - prev) / prev * 100, 2)
            if curr is not None and prev else None
        )
        if curr is None:
            regime = None
        elif curr < 15:
            regime = "complacent"
        elif curr < 20:
            regime = "normal"
        elif curr < 30:
            regime = "elevated"
        else:
            regime = "fear/crisis"
        vix = {
            "symbol":         "VIX",
            "current_level":  curr,
            "prev_level":     prev,
            "day_change_pct": vix_pct,
            "regime":         regime,
            "pct_color":      _pct_color(vix_pct),
            "formatted_pct":  _fmt_pct(vix_pct),
            "data_source":    vix_bars.get("data_source"),
        }

    # --- Knowledge ----------------------------------------------------------
    knowledge: dict = {}
    for (key, _, _), result in zip(_KNOWLEDGE_QUERIES, kn_results):
        knowledge[key] = result if not isinstance(result, Exception) else {"error": str(result)}

    now = datetime.now(timezone.utc)
    analysis_date = f"{now.strftime('%B')} {_ordinal(now.day)}, {now.year}"

    return {
        "analysis_type":  "market_analysis",
        "analysis_date":  analysis_date,
        "market_open":    _is_market_open(),
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
