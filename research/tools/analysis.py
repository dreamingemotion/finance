"""
Stock analysis tool — aggregates market data, filings, and knowledge base
into a single structured result for Claude to synthesize.

Full analysis (default):
  Phase 1 (parallel): get_snapshot, get_full_timeframe, search_knowledge
  Phase 2 (sequential): submit_filing for the most recent 10-K
  Phase 3 (parallel): search_filing for risks, competitive moat, cash flow

Partial analysis:
  Single parallel gather: get_snapshot, get_bars(1y/1wk), search_knowledge
"""
from __future__ import annotations

import asyncio
from datetime import date

from research.tools.knowledge import search_knowledge
from research.tools.market_data import get_bars, get_full_timeframe, get_snapshot
from research.tools.sec_filings import search_filing, submit_filing


def _ok(result: object) -> object:
    return {"error": str(result)} if isinstance(result, Exception) else result


async def _submit_recent_10k(symbol: str) -> dict:
    year = date.today().year
    for y in [year, year - 1]:
        try:
            return await submit_filing(symbol, "10-K", y)
        except Exception:
            continue
    return {"error": f"No 10-K found for {symbol} in {year} or {year - 1}"}


async def analyze(symbol: str, full: bool = True) -> dict:
    symbol = symbol.upper()

    if not full:
        snapshot, bars, knowledge = await asyncio.gather(
            get_snapshot(symbol),
            get_bars(symbol, "1y", "1wk"),
            search_knowledge(f"{symbol} analysis", limit=5),
            return_exceptions=True,
        )
        return {
            "symbol":          symbol,
            "analysis_type":   "partial",
            "snapshot":        _ok(snapshot),
            "price_structure": _ok(bars),
            "knowledge":       _ok(knowledge),
        }

    # Full — phase 1
    snapshot, timeframes, knowledge = await asyncio.gather(
        get_snapshot(symbol),
        get_full_timeframe(symbol),
        search_knowledge(f"{symbol} analysis", limit=5),
        return_exceptions=True,
    )

    # Full — phase 2: filing (sequential; need doc_id before searching)
    filing_meta = await _submit_recent_10k(symbol)
    doc_id = filing_meta.get("doc_id") if "error" not in filing_meta else None

    if doc_id:
        risks, moat, cashflow = await asyncio.gather(
            search_filing("risk factors", doc_id),
            search_filing("competitive advantage economic moat business model", doc_id),
            search_filing("cash flow free cash flow capital allocation", doc_id),
            return_exceptions=True,
        )
        filing = {
            **filing_meta,
            "risks":    _ok(risks),
            "moat":     _ok(moat),
            "cashflow": _ok(cashflow),
        }
    else:
        filing = filing_meta

    return {
        "symbol":          symbol,
        "analysis_type":   "full",
        "snapshot":        _ok(snapshot),
        "price_structure": _ok(timeframes),
        "knowledge":       _ok(knowledge),
        "filing":          filing,
    }
