"""
yfinance market data provider (secondary / fallback).

All yfinance calls are blocking; they run in a thread executor so they
don't block the async event loop.
"""
from __future__ import annotations

import asyncio
from datetime import timezone

import yfinance as yf

from .base import MarketDataProvider


async def _run(fn):
    return await asyncio.to_thread(fn)


class YFinanceProvider(MarketDataProvider):

    async def get_quote(self, symbol: str) -> dict:
        def _fetch():
            info = yf.Ticker(symbol).info or {}
            return {
                "symbol":     symbol,
                "bid":        info.get("bid"),
                "ask":        info.get("ask"),
                "last":       info.get("currentPrice") or info.get("regularMarketPrice"),
                "mark":       None,
                "open":       info.get("open") or info.get("regularMarketOpen"),
                "high":       info.get("dayHigh") or info.get("regularMarketDayHigh"),
                "low":        info.get("dayLow") or info.get("regularMarketDayLow"),
                "close":      info.get("previousClose") or info.get("regularMarketPreviousClose"),
                "volume":     info.get("volume") or info.get("regularMarketVolume"),
                "updated_at": None,
            }
        return await _run(_fetch)

    async def get_metrics(self, symbol: str) -> dict:
        def _fetch():
            info = yf.Ticker(symbol).info or {}
            return {
                "symbol":         symbol,
                "pe_ratio":       info.get("trailingPE") or info.get("forwardPE"),
                "pb_ratio":       info.get("priceToBook"),
                "eps":            info.get("trailingEps"),
                "market_cap":     info.get("marketCap"),
                "iv_rank":        None,
                "iv_index":       None,
                "hv_30d":         None,
                "hv_60d":         None,
                "beta":           info.get("beta"),
                "dividend_yield": info.get("dividendYield"),
                "borrow_rate":    None,
            }
        return await _run(_fetch)

    async def get_pb_ratio(self, symbol: str) -> float | None:
        def _fetch():
            return (yf.Ticker(symbol).info or {}).get("priceToBook")
        return await _run(_fetch)

    async def get_bars(self, symbol: str, period: str, interval: str) -> list[dict]:
        def _fetch():
            df = yf.Ticker(symbol).history(
                period=period, interval=interval, auto_adjust=True
            )
            bars = []
            for ts, row in df.iterrows():
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                bars.append({
                    "time":   dt.astimezone(timezone.utc).isoformat(),
                    "open":   float(row["Open"]),
                    "high":   float(row["High"]),
                    "low":    float(row["Low"]),
                    "close":  float(row["Close"]),
                    "volume": float(row["Volume"]),
                })
            return bars
        return await _run(_fetch)
