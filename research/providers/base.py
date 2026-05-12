"""
Abstract market data provider, shared models, and market-hours utilities.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel

_ET = ZoneInfo("America/New_York")


class Bar(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


def get_last_market_close(now: datetime) -> datetime:
    """Return the most recent 4 PM ET equity market close as a UTC datetime."""
    now_et = now.astimezone(_ET)
    close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_et < close:
        close -= timedelta(days=1)
    while close.weekday() >= 5:  # step back over weekends
        close -= timedelta(days=1)
    return close.astimezone(timezone.utc)


def is_stale(bar: Bar) -> bool:
    """A bar is stale if it's from before the most recent close."""
    now = datetime.now(timezone.utc)
    market_close = get_last_market_close(now)
    return bar.time.date() < market_close.date()


def is_quote_stale(updated_at: datetime | None) -> bool:
    """A quote is stale if its timestamp predates the most recent market close."""
    if updated_at is None:
        return False  # unknown — don't assume stale
    now = datetime.now(timezone.utc)
    market_close = get_last_market_close(now)
    ts = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
    return ts < market_close


class MarketDataProvider(ABC):
    """
    Abstract base class shared by TastytradeProvider and YFinanceProvider.

    Both providers implement the same method signatures so the fallback
    logic in tools/market_data.py can swap them transparently.
    """

    @abstractmethod
    async def get_quote(self, symbol: str) -> dict:
        """Real-time quote: price, bid, ask, mark, day OHLCV, volume."""
        ...

    @abstractmethod
    async def get_metrics(self, symbol: str) -> dict:
        """Extended metrics: P/E, IV rank, HV, beta, market cap, dividends."""
        ...

    @abstractmethod
    async def get_bars(self, symbol: str, period: str, interval: str) -> list[dict]:
        """
        OHLCV bars sorted oldest-first.

        period:   1d 5d 1mo 3mo 6mo 1y 2y 5y
        interval: 1m 5m 15m 30m 1h 1d 1wk 1mo
        """
        ...
