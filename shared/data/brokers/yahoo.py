"""
Self-contained Yahoo Finance data grabber.

Wraps yfinance for quotes, fundamentals, financials, options, news, and earnings.
Call register_tools(mcp) from transport.py.

No credentials required — yfinance uses public Yahoo Finance endpoints.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol.upper())


def _safe(val: Any) -> Any:
    """Convert NaN floats to None for JSON serialization."""
    if val is None:
        return None
    try:
        if isinstance(val, float) and math.isnan(val):
            return None
    except Exception:
        pass
    return val


def _safe_df(df: pd.DataFrame) -> dict:
    """
    Convert a DataFrame to a fully JSON-serializable dict.

    Keys (columns and index) are stringified; NaN → None;
    numpy scalars are unwrapped to Python primitives.
    """
    result: dict = {}
    for col, row_dict in df.to_dict().items():
        col_str = str(col)
        result[col_str] = {}
        for idx, val in row_dict.items():
            idx_str = str(idx)
            if isinstance(val, float) and math.isnan(val):
                result[col_str][idx_str] = None
            elif hasattr(val, "item"):
                result[col_str][idx_str] = val.item()
            else:
                result[col_str][idx_str] = val
    return result


def _safe_records(df: pd.DataFrame) -> list[dict]:
    """Convert a DataFrame to a list of records with all values JSON-safe."""
    records = []
    for record in df.to_dict(orient="records"):
        safe_record: dict = {}
        for k, v in record.items():
            k_str = str(k)
            if isinstance(v, float) and math.isnan(v):
                safe_record[k_str] = None
            elif hasattr(v, "item"):
                safe_record[k_str] = v.item()
            else:
                safe_record[k_str] = str(v) if not isinstance(v, (int, float, bool, str, type(None))) else v
        records.append(safe_record)
    return records


# ---------------------------------------------------------------------------
# Earnings core logic (shared by register_tools and build_fallback_fns)
# ---------------------------------------------------------------------------

def _get_earnings_raw(symbol: str, limit: int = 24) -> dict:
    """
    Core earnings fetch logic.  Returns the same shape as yf_get_earnings
    without any _note field — callers add that if needed.
    """
    t = _ticker(symbol)
    info = t.info
    trailing_eps = _safe(info.get("trailingEps"))
    trailing_pe = _safe(info.get("trailingPE"))
    quarterly_eps: list[dict] = []

    try:
        get_fn = getattr(t, "get_earnings_dates", None)
        ed = get_fn(limit=min(limit, 40)) if get_fn else t.earnings_dates
        if ed is not None and not ed.empty and "Reported EPS" in ed.columns:
            reported = ed["Reported EPS"].dropna()
            for ts, eps_val in reported.items():
                if pd.notna(eps_val):
                    ts_naive = ts.tz_convert(None) if ts.tzinfo is not None else ts
                    quarterly_eps.append({
                        "date": ts_naive.date().isoformat(),
                        "eps":  round(float(eps_val), 4),
                    })
    except Exception:
        pass

    if not quarterly_eps:
        try:
            qf = t.quarterly_financials
            if qf is not None and not qf.empty:
                ni = None
                for row_name in ("Net Income", "Net Income Common Stockholders"):
                    if row_name in qf.index:
                        ni = qf.loc[row_name].dropna()
                        break
                if ni is not None and not ni.empty:
                    shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
                    if shares and shares > 0:
                        for ts, net_income in ni.items():
                            if pd.notna(net_income):
                                ts_naive = ts.tz_convert(None) if ts.tzinfo is not None else ts
                                quarterly_eps.append({
                                    "date": ts_naive.date().isoformat(),
                                    "eps":  round(float(net_income) / float(shares), 4),
                                })
        except Exception:
            pass

    quarterly_eps.sort(key=lambda x: x["date"])

    if trailing_eps is None and trailing_pe:
        try:
            last_price = getattr(t.fast_info, "last_price", None)
            if last_price and trailing_pe > 0:
                trailing_eps = round(float(last_price) / float(trailing_pe), 4)
        except Exception:
            pass

    return {
        "symbol":         symbol.upper(),
        "trailing_eps":   trailing_eps,
        "trailing_pe":    trailing_pe,
        "quarterly_eps":  quarterly_eps,
        "is_approximate": not bool(quarterly_eps),
    }


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def register_tools(mcp: Any) -> None:
    """Register all Yahoo Finance MCP tools on the provided FastMCP instance."""

    # ---- Quote -------------------------------------------------------------

    @mcp.tool()
    def yf_get_quote(symbol: str) -> dict:
        """
        Get current price, change, volume, and day high/low for a symbol.

        Uses fast_info for a lightweight fetch — no full fundamentals loaded.
        """
        t = _ticker(symbol)
        fi = t.fast_info
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        chg = (last - prev) if (last is not None and prev is not None) else None
        pct = (chg / prev * 100) if (chg is not None and prev) else None
        return {
            "symbol":     symbol.upper(),
            "last":       last,
            "prev_close": prev,
            "change":     round(chg, 4) if chg is not None else None,
            "change_pct": round(pct, 4) if pct is not None else None,
            "day_high":   getattr(fi, "day_high", None),
            "day_low":    getattr(fi, "day_low", None),
            "volume":     getattr(fi, "last_volume", None),
            "market_cap": getattr(fi, "market_cap", None),
        }

    # ---- Info (fundamentals) -----------------------------------------------

    @mcp.tool()
    def yf_get_info(symbol: str) -> dict:
        """
        Get full company profile and fundamentals: PE ratio, EPS, market cap,
        sector, margins, analyst ratings, dividend info, 52-week range, and more.
        """
        info = _ticker(symbol).info
        keys = [
            "shortName", "longName", "sector", "industry", "country",
            "website", "longBusinessSummary",
            "marketCap", "enterpriseValue",
            "trailingPE", "forwardPE", "priceToBook", "priceToSalesTrailing12Months",
            "trailingEps", "forwardEps",
            "dividendRate", "dividendYield", "exDividendDate", "payoutRatio",
            "beta", "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
            "fiftyDayAverage", "twoHundredDayAverage",
            "averageVolume", "averageVolume10days",
            "totalRevenue", "grossMargins", "operatingMargins", "profitMargins",
            "returnOnEquity", "returnOnAssets",
            "totalDebt", "totalCash", "freeCashflow",
            "revenueGrowth", "earningsGrowth",
            "recommendationMean", "recommendationKey", "numberOfAnalystOpinions",
            "targetHighPrice", "targetLowPrice", "targetMeanPrice",
            "currency", "exchange", "quoteType",
        ]
        return {k: _safe(info.get(k)) for k in keys if info.get(k) is not None}

    # ---- Price history -----------------------------------------------------

    @mcp.tool()
    def yf_get_history(
        symbol: str,
        period: str = "1mo",
        interval: str = "1d",
        prepost: bool = False,
    ) -> dict:
        """
        Get OHLCV price history for a symbol.

        period:   1d | 5d | 1mo | 3mo | 6mo | 1y | 2y | 5y | 10y | ytd | max
        interval: 1m | 2m | 5m | 15m | 30m | 60m | 90m | 1h | 1d | 5d | 1wk | 1mo | 3mo
        prepost:  include pre-market and post-market data
        """
        df = _ticker(symbol).history(period=period, interval=interval, prepost=prepost)
        bars = [
            {
                "time":   str(ts),
                "open":   round(row.Open, 4),
                "high":   round(row.High, 4),
                "low":    round(row.Low, 4),
                "close":  round(row.Close, 4),
                "volume": int(row.Volume),
            }
            for ts, row in df.iterrows()
        ]
        return {"symbol": symbol.upper(), "period": period, "interval": interval, "bars": bars}

    # ---- Financials --------------------------------------------------------

    @mcp.tool()
    def yf_get_financials(symbol: str) -> dict:
        """Get annual income statement (revenue, gross profit, net income, EBITDA, etc.)."""
        df = _ticker(symbol).financials
        return {"symbol": symbol.upper(), "income_statement": _safe_df(df)}

    @mcp.tool()
    def yf_get_balance_sheet(symbol: str) -> dict:
        """Get annual balance sheet (assets, liabilities, equity, cash, debt, etc.)."""
        df = _ticker(symbol).balance_sheet
        return {"symbol": symbol.upper(), "balance_sheet": _safe_df(df)}

    @mcp.tool()
    def yf_get_cashflow(symbol: str) -> dict:
        """Get annual cash flow statement (operating, investing, financing activities)."""
        df = _ticker(symbol).cashflow
        return {"symbol": symbol.upper(), "cashflow": _safe_df(df)}

    # ---- Dividends & splits ------------------------------------------------

    @mcp.tool()
    def yf_get_dividends(symbol: str) -> dict:
        """Get historical dividend payments (date → amount per share)."""
        s = _ticker(symbol).dividends
        return {"symbol": symbol.upper(), "dividends": {str(k): v for k, v in s.items()}}

    @mcp.tool()
    def yf_get_splits(symbol: str) -> dict:
        """Get historical stock splits (date → split ratio)."""
        s = _ticker(symbol).splits
        return {"symbol": symbol.upper(), "splits": {str(k): v for k, v in s.items()}}

    # ---- Analyst recommendations & news ------------------------------------

    @mcp.tool()
    def yf_get_recommendations(symbol: str) -> dict:
        """Get the most recent analyst buy/sell/hold recommendations (last 20)."""
        df = _ticker(symbol).recommendations
        if df is None or df.empty:
            return {"symbol": symbol.upper(), "recommendations": []}
        return {"symbol": symbol.upper(), "recommendations": _safe_records(df.tail(20))}

    @mcp.tool()
    def yf_get_news(symbol: str) -> dict:
        """Get recent news articles (up to 10) for a symbol."""
        articles = _ticker(symbol).news or []
        return {
            "symbol": symbol.upper(),
            "news": [
                {
                    "title":     a.get("content", {}).get("title", ""),
                    "publisher": a.get("content", {}).get("provider", {}).get("displayName", ""),
                    "link":      a.get("content", {}).get("canonicalUrl", {}).get("url", ""),
                    "published": a.get("content", {}).get("pubDate", ""),
                }
                for a in articles[:10]
            ],
        }

    # ---- Options -----------------------------------------------------------

    @mcp.tool()
    def yf_get_option_expirations(symbol: str) -> dict:
        """Get available options expiration dates for a symbol."""
        return {"symbol": symbol.upper(), "expirations": list(_ticker(symbol).options)}

    @mcp.tool()
    def yf_get_option_chain(symbol: str, expiration: str) -> dict:
        """
        Get calls and puts for a given expiration date.

        expiration: YYYY-MM-DD (use yf_get_option_expirations to list valid dates)
        """
        chain = _ticker(symbol).option_chain(expiration)
        return {
            "symbol":     symbol.upper(),
            "expiration": expiration,
            "calls":      _safe_records(chain.calls),
            "puts":       _safe_records(chain.puts),
        }

    # ---- Earnings / EPS ----------------------------------------------------

    @mcp.tool()
    def yf_get_earnings(symbol: str, limit: int = 24) -> dict:
        """
        Get quarterly EPS history sorted ascending (oldest first).

        Useful for building trailing-twelve-month (TTM) P/E ratio series.
        Falls back to quarterly net income ÷ shares outstanding when
        earnings_dates is unavailable (e.g. ETFs), and derives trailing_eps
        from trailingPE × price as a last resort.

        limit: number of quarterly periods to request (default 24 = 6 years)
        """
        return _get_earnings_raw(symbol, limit)


# ---------------------------------------------------------------------------
# Fallback callables for tastytrade.register_tools(fallbacks=...)
# ---------------------------------------------------------------------------

_STALE = "Note: this analysis uses delayed data because the live feed is down."

# TT candle period → yfinance interval (best available match)
_INTERVAL_MAP = {
    "1m": "1m",  "2m": "2m",  "3m": "5m",  "5m": "5m",
    "10m": "15m", "15m": "15m", "30m": "30m",
    "1h": "1h",  "2h": "1h",  "4h": "1h",
    "1d": "1d",  "1w": "1wk", "1mo": "1mo",
}


def build_fallback_fns() -> dict[str, Any]:
    """
    Return adapted callables that match Tastytrade tool signatures but source
    data from Yahoo Finance.  Pass the result to tastytrade.register_tools().

    Each function returns data in the closest equivalent shape to the TT tool,
    with a top-level "_note" key carrying the stale-data warning.
    """

    def _quote_core(symbol: str) -> dict:
        t = _ticker(symbol)
        fi = t.fast_info
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        return {
            "symbol":        symbol.upper(),
            "bid":           None,
            "ask":           None,
            "last":          last,
            "mark":          last,
            "open":          getattr(fi, "open", None),
            "high":          getattr(fi, "day_high", None),
            "low":           getattr(fi, "day_low", None),
            "close":         prev,
            "volume":        getattr(fi, "last_volume", None),
        }

    def tt_get_quote(symbol: str, instrument_type: str = "equity") -> dict:
        return {**_quote_core(symbol), "_note": _STALE}

    def tt_get_quotes_by_type(
        equities: list | None = None,
        equity_options: list | None = None,
        futures: list | None = None,
        future_options: list | None = None,
        cryptocurrencies: list | None = None,
        indices: list | None = None,
    ) -> dict:
        symbols = (
            list(equities or []) + list(indices or []) + list(cryptocurrencies or [])
        )
        items = []
        for s in symbols:
            try:
                items.append(_quote_core(s))
            except Exception:
                pass
        return {"_note": _STALE, "items": items}

    def tt_stream_quotes(symbols: list, duration_seconds: float = 5.0) -> dict:
        items = []
        for s in symbols:
            try:
                items.append(_quote_core(s))
            except Exception:
                pass
        return {"_note": _STALE, "items": items}

    def tt_stream_candles(
        symbols: list,
        period: str = "1d",
        from_date: str | None = None,
        duration_seconds: float = 10.0,
        regular_hours_only: bool = False,
    ) -> dict:
        yf_interval = _INTERVAL_MAP.get(period, "1d")
        all_bars = []
        for symbol in symbols:
            try:
                t = _ticker(symbol)
                df = (
                    t.history(interval=yf_interval, start=from_date)
                    if from_date
                    else t.history(interval=yf_interval, period="max")
                )
                for ts, row in df.iterrows():
                    all_bars.append({
                        "event_symbol": symbol.upper(),
                        "time":  int(ts.timestamp() * 1000),
                        "open":  round(float(row.Open), 4),
                        "high":  round(float(row.High), 4),
                        "low":   round(float(row.Low), 4),
                        "close": round(float(row.Close), 4),
                        "volume": int(row.Volume),
                    })
            except Exception:
                pass
        return {"_note": _STALE, "items": all_bars}

    def tt_get_market_metrics(symbols: list) -> dict:
        items = []
        for symbol in symbols:
            try:
                info = _ticker(symbol).info
                items.append({
                    "symbol":                     symbol.upper(),
                    "beta":                       _safe(info.get("beta")),
                    "market_cap":                 _safe(info.get("marketCap")),
                    "price_earnings_ratio":       _safe(info.get("trailingPE")),
                    "earnings_per_share":         _safe(info.get("trailingEps")),
                    "dividend_rate_per_share":    _safe(info.get("dividendRate")),
                    "dividend_yield":             _safe(info.get("dividendYield")),
                    "annualized_dividend":        _safe(info.get("dividendRate")),
                    # IV rank/percentile/HV not available from Yahoo
                    "implied_volatility_rank":        None,
                    "implied_volatility_percentile":  None,
                    "historical_volatility_30_day":   None,
                })
            except Exception:
                pass
        return {"_note": _STALE, "items": items}

    def tt_get_dividends(symbol: str) -> dict:
        s = _ticker(symbol).dividends
        items = [
            {"occurred_date": str(k).split(" ")[0], "amount": str(round(float(v), 6))}
            for k, v in s.items()
        ]
        return {"_note": _STALE, "items": items}

    def tt_get_earnings(symbol: str, start_date: str | None = None) -> dict:
        result = _get_earnings_raw(symbol)
        if start_date:
            result["quarterly_eps"] = [
                e for e in result["quarterly_eps"] if e["date"] >= start_date
            ]
        result["_note"] = _STALE
        return result

    def tt_get_option_chain(underlying_symbol: str) -> dict:
        t = _ticker(underlying_symbol)
        expirations = list(t.options)[:8]  # nearest 8 expirations
        chain: dict[str, list] = {}
        for exp in expirations:
            try:
                oc = t.option_chain(exp)
                calls = _safe_records(oc.calls)
                puts = _safe_records(oc.puts)
                chain[exp] = [
                    {
                        "symbol":           row.get("contractSymbol", ""),
                        "underlying_symbol": underlying_symbol.upper(),
                        "option_type":      "C",
                        "expiration_date":  exp,
                        "strike_price":     str(row.get("strike", "")),
                        "bid":              row.get("bid"),
                        "ask":              row.get("ask"),
                        "last_price":       row.get("lastPrice"),
                        "volume":           row.get("volume"),
                        "open_interest":    row.get("openInterest"),
                        "implied_volatility": row.get("impliedVolatility"),
                    }
                    for row in calls
                ] + [
                    {
                        "symbol":           row.get("contractSymbol", ""),
                        "underlying_symbol": underlying_symbol.upper(),
                        "option_type":      "P",
                        "expiration_date":  exp,
                        "strike_price":     str(row.get("strike", "")),
                        "bid":              row.get("bid"),
                        "ask":              row.get("ask"),
                        "last_price":       row.get("lastPrice"),
                        "volume":           row.get("volume"),
                        "open_interest":    row.get("openInterest"),
                        "implied_volatility": row.get("impliedVolatility"),
                    }
                    for row in puts
                ]
            except Exception:
                pass
        return {"_note": _STALE, **chain}

    return {
        "tt_get_quote":           tt_get_quote,
        "tt_get_quotes_by_type":  tt_get_quotes_by_type,
        "tt_stream_quotes":       tt_stream_quotes,
        "tt_stream_candles":      tt_stream_candles,
        "tt_get_market_metrics":  tt_get_market_metrics,
        "tt_get_dividends":       tt_get_dividends,
        "tt_get_earnings":        tt_get_earnings,
        "tt_get_option_chain":    tt_get_option_chain,
    }
