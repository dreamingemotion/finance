"""
Valuation ratio tools.

get_valuation_ratios(symbol):
  - 10-year historical P/E (diluted EPS from EDGAR XBRL, year-end price from yfinance)
  - 10-year historical P/B (equity + shares from EDGAR XBRL, year-end price from yfinance)
  - Sector P/E and P/B via the corresponding SPDR sector ETF from yfinance

EDGAR XBRL company-facts API provides structured financial time-series data
going back 10+ years without requiring filing downloads or LLM extraction.

Environment variables:
  EDGAR_USER_AGENT   required by SEC (shared with sec_filings tools)
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import httpx
import yfinance as yf

from research.edgar import resolve_cik

_XBRL_BASE = "https://data.sec.gov/api/xbrl/companyfacts"

# yfinance sector string → SPDR sector ETF
_SECTOR_ETF: dict[str, str] = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Healthcare":             "XLV",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}


def _headers() -> dict[str, str]:
    agent = os.environ.get("EDGAR_USER_AGENT", "FinanceResearchMCP contact@example.com")
    return {"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}


async def _fetch_xbrl(cik: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_XBRL_BASE}/CIK{cik}.json",
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
    return resp.json()


def _annual_10k(facts: dict, concept: str, unit: str) -> list[dict]:
    """
    Extract annual 10-K entries for a XBRL concept/unit, deduped by fiscal year.
    Returns entries sorted oldest-first, limited to the last 10 fiscal years.
    Keeps the latest amendment when a FY appears more than once.
    """
    entries = (
        facts.get("us-gaap", {})
             .get(concept, {})
             .get("units", {})
             .get(unit, [])
    )
    annual = [e for e in entries if e.get("fp") == "FY" and "10-K" in e.get("form", "")]
    by_fy: dict[int, dict] = {}
    for e in annual:
        fy = e.get("fy")
        if fy is None:
            continue
        if fy not in by_fy or e.get("filed", "") > by_fy[fy].get("filed", ""):
            by_fy[fy] = e
    cutoff = date.today().year - 10
    return sorted(
        (e for e in by_fy.values() if e["fy"] >= cutoff),
        key=lambda x: x["fy"],
    )


def _price_near(prices: dict[str, float], target: str) -> float | None:
    """Closest trading-day close on or before target (searches up to 10 days back, then 10 forward)."""
    t = date.fromisoformat(target)
    for d in range(0, 11):
        p = prices.get((t - timedelta(days=d)).isoformat())
        if p is not None:
            return p
    for d in range(1, 11):
        p = prices.get((t + timedelta(days=d)).isoformat())
        if p is not None:
            return p
    return None


def _r(val: object, n: int = 2) -> float | None:
    try:
        return round(float(val), n) if val is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _yf_info_sync(symbol: str) -> dict:
    info = yf.Ticker(symbol).info
    return {
        "short_name":    info.get("shortName"),
        "sector":        info.get("sector"),
        "industry":      info.get("industry"),
        "trailing_pe":   info.get("trailingPE"),
        "price_to_book": info.get("priceToBook"),
    }


def _price_history_sync(symbol: str) -> dict[str, float]:
    df = yf.Ticker(symbol).history(period="10y", interval="1d")
    return {str(ts.date()): round(float(row.Close), 4) for ts, row in df.iterrows()}


def _etf_ratios_sync(etf: str) -> dict:
    info = yf.Ticker(etf).info
    return {
        "trailing_pe":   info.get("trailingPE"),
        "price_to_book": info.get("priceToBook"),
    }


async def get_valuation_ratios(symbol: str) -> dict:
    symbol = symbol.upper()
    notes: list[str] = []

    # Phase 1: CIK, company info, 10-year price history — all parallel
    cik_r, info_r, prices_r = await asyncio.gather(
        resolve_cik(symbol),
        asyncio.to_thread(_yf_info_sync, symbol),
        asyncio.to_thread(_price_history_sync, symbol),
        return_exceptions=True,
    )

    if isinstance(cik_r, Exception):
        return {"error": f"Ticker not found in EDGAR: {cik_r}"}

    cik    = cik_r
    info   = info_r   if not isinstance(info_r,   Exception) else {}
    prices = prices_r if not isinstance(prices_r, Exception) else {}

    if isinstance(info_r,   Exception): notes.append("yfinance company info unavailable")
    if isinstance(prices_r, Exception): notes.append("yfinance price history unavailable")

    sector = info.get("sector") if isinstance(info, dict) else None
    etf    = _SECTOR_ETF.get(sector) if sector else None

    # Phase 2: XBRL company facts + sector ETF — parallel
    phase2_args: list = [_fetch_xbrl(cik)]
    if etf:
        phase2_args.append(asyncio.to_thread(_etf_ratios_sync, etf))

    phase2 = await asyncio.gather(*phase2_args, return_exceptions=True)
    xbrl_r = phase2[0]
    etf_r  = phase2[1] if etf else None

    if isinstance(xbrl_r, Exception):
        return {"error": f"EDGAR XBRL data unavailable: {xbrl_r}"}

    facts = xbrl_r.get("facts", {})

    # --- Debt and cash ---
    lt_noncurrent_entries = _annual_10k(facts, "LongTermDebtNoncurrent", "USD")
    debt_current_entries  = _annual_10k(facts, "DebtCurrent", "USD")
    lt_debt_entries       = _annual_10k(facts, "LongTermDebt", "USD")
    cash_entries          = _annual_10k(facts, "CashAndCashEquivalentsAtCarryingValue", "USD")

    lt_noncurrent_by_fy = {e["fy"]: e["val"] for e in lt_noncurrent_entries}
    debt_current_by_fy  = {e["fy"]: e["val"] for e in debt_current_entries}
    lt_debt_by_fy       = {e["fy"]: e["val"] for e in lt_debt_entries}
    cash_by_fy          = {e["fy"]: e["val"] for e in cash_entries}

    all_debt_fys = sorted(
        set(lt_noncurrent_by_fy) | set(lt_debt_by_fy) | set(cash_by_fy)
    )
    debt_history: list[dict] = []
    for fy in all_debt_fys:
        # Prefer noncurrent + current split; fall back to LongTermDebt lump
        lt   = lt_noncurrent_by_fy.get(fy)
        curr = debt_current_by_fy.get(fy)
        lt_all = lt_debt_by_fy.get(fy)
        if lt is not None:
            total = lt + (curr or 0)
        elif lt_all is not None:
            total = lt_all
        else:
            total = None
        cash    = cash_by_fy.get(fy)
        net_debt = (total - cash) if (total is not None and cash is not None) else None
        debt_history.append({
            "year":       fy,
            "lt_debt_M":  _r(lt_all / 1e6, 1) if lt_all is not None else _r(total / 1e6, 1) if total is not None else None,
            "cash_M":     _r(cash   / 1e6, 1) if cash  is not None else None,
            "net_debt_M": _r(net_debt / 1e6, 1) if net_debt is not None else None,
        })

    # --- Free cash flow components ---
    ocf_entries  = _annual_10k(facts, "NetCashProvidedByUsedInOperatingActivities", "USD")
    capex_entries = _annual_10k(facts, "PaymentsToAcquirePropertyPlantAndEquipment", "USD")
    capex_by_fy  = {e["fy"]: e["val"] for e in capex_entries}

    fcf_history: list[dict] = []
    for e in ocf_entries:
        fy  = e["fy"]
        ocf = e["val"]
        capex = capex_by_fy.get(fy)
        fcf   = (ocf - capex) if capex is not None else None
        fcf_history.append({
            "year":  fy,
            "ocf_M": _r(ocf   / 1e6, 1),
            "capex_M": _r(capex / 1e6, 1) if capex is not None else None,
            "fcf_M": _r(fcf   / 1e6, 1) if fcf  is not None else None,
        })

    # --- EPS: diluted preferred, basic as fallback ---
    eps_entries = _annual_10k(facts, "EarningsPerShareDiluted", "USD/shares")
    if not eps_entries:
        eps_entries = _annual_10k(facts, "EarningsPerShareBasic", "USD/shares")
        if eps_entries:
            notes.append("EarningsPerShareBasic used (diluted not found in XBRL)")

    # --- Book value components ---
    equity_entries = (
        _annual_10k(facts, "StockholdersEquity", "USD") or
        _annual_10k(facts, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD")
    )
    shares_entries = (
        _annual_10k(facts, "CommonStockSharesOutstanding", "shares") or
        _annual_10k(facts, "SharesOutstanding", "shares")
    )
    equity_by_fy = {e["fy"]: e for e in equity_entries}
    shares_by_fy = {e["fy"]: e["val"] for e in shares_entries}

    # --- P/E history ---
    pe_history: list[dict] = []
    for e in eps_entries:
        fy, eps, end = e["fy"], e["val"], e["end"]
        price = _price_near(prices, end)
        pe    = _r(price / eps) if (price and eps and eps > 0) else None
        pe_history.append({
            "year":            fy,
            "fiscal_year_end": end,
            "eps":             _r(eps, 4),
            "price":           _r(price),
            "pe":              pe,
        })

    # --- P/B history ---
    pb_history: list[dict] = []
    for fy in sorted(set(equity_by_fy) & set(shares_by_fy)):
        eq_entry = equity_by_fy[fy]
        equity   = eq_entry["val"]
        shares   = shares_by_fy[fy]
        if not equity or not shares or shares <= 0:
            continue
        bvps  = equity / shares
        price = _price_near(prices, eq_entry["end"])
        pb    = _r(price / bvps) if (price and bvps != 0) else None
        pb_history.append({
            "year":            fy,
            "fiscal_year_end": eq_entry["end"],
            "equity_M":        _r(equity / 1e6, 1),
            "shares_M":        _r(shares / 1e6, 2),
            "bvps":            _r(bvps, 4),
            "price":           _r(price),
            "pb":              pb,
        })

    # --- 10-year averages (exclude loss years from P/E, negative equity from P/B) ---
    pe_vals = [r["pe"] for r in pe_history if r["pe"] is not None and r["pe"] > 0]
    pb_vals = [r["pb"] for r in pb_history if r["pb"] is not None and r["pb"] > 0]

    # --- Sector benchmark ---
    benchmark: dict = {"etf": etf, "sector": sector}
    if isinstance(etf_r, dict):
        benchmark["sector_pe"] = _r(etf_r.get("trailing_pe"))
        benchmark["sector_pb"] = _r(etf_r.get("price_to_book"))
    elif etf_r is not None:
        notes.append(f"Sector ETF ({etf}) metrics unavailable")

    return {
        "symbol":           symbol,
        "company_name":     info.get("short_name") if isinstance(info, dict) else None,
        "sector":           sector,
        "industry":         info.get("industry") if isinstance(info, dict) else None,
        "pe_history":       pe_history,
        "pe_average":       _r(sum(pe_vals) / len(pe_vals)) if pe_vals else None,
        "pe_current":       _r(info.get("trailing_pe")) if isinstance(info, dict) else None,
        "pb_history":       pb_history,
        "pb_average":       _r(sum(pb_vals) / len(pb_vals)) if pb_vals else None,
        "pb_current":       _r(info.get("price_to_book")) if isinstance(info, dict) else None,
        "fcf_history":      fcf_history,
        "debt_history":     debt_history,
        "sector_benchmark": benchmark,
        "data_years":       {"pe": len(pe_history), "pb": len(pb_history), "fcf": len(fcf_history), "debt": len(debt_history)},
        "data_source":      "EDGAR XBRL + yfinance",
        "notes":            notes,
    }
