"""
Self-contained Tastytrade data grabber.

Includes auth, HTTP client, DXLink WebSocket streamer, Pydantic models,
and MCP tool registration. Call register_tools(mcp) from transport.py.

Required environment variables:
    TT_CLIENT_ID       — OAuth2 client ID
    TT_CLIENT_SECRET   — OAuth2 client secret
    TT_REFRESH_TOKEN   — OAuth2 refresh token (never expires)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncGenerator, ClassVar, Iterable

import httpx
from pydantic import BaseModel, Field, field_validator
from pydantic.alias_generators import to_camel
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://api.tastytrade.com"
_TOKEN_PATH = "/oauth/token"
_TOKEN_BUFFER_SECS = 60
_DXLINK_VERSION = "0.1-DXF-JS/0.3.0"
_KEEPALIVE_INTERVAL = 30


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TastytradeError(Exception):
    pass


class TastytradeAPIError(TastytradeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {body}")


class TastytradeAuthError(TastytradeError):
    pass


class TastytradeStreamError(TastytradeError):
    pass


# ---------------------------------------------------------------------------
# Auth — OAuth2 refresh-token grant, auto-renews 60 s before expiry
# ---------------------------------------------------------------------------

class TastytradeAuth:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str = ""
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if time.monotonic() < self._expires_at - _TOKEN_BUFFER_SECS:
                return self._access_token
            await self._refresh()
            return self._access_token

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(base_url=_BASE_URL) as http:
            resp = await http.post(
                _TOKEN_PATH,
                json={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
            )
        if resp.is_error:
            raise TastytradeAuthError(
                f"Token refresh failed ({resp.status_code}): {resp.text}"
            )
        body = resp.json()
        self._access_token = body["access_token"]
        self._expires_at = time.monotonic() + body.get("expires_in", 900)


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

class TastytradeClient:
    def __init__(self, auth: TastytradeAuth) -> None:
        self._auth = auth
        self._http = httpx.AsyncClient(base_url=_BASE_URL, timeout=30.0)

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._auth.get_token()
        return {"Authorization": f"Bearer {token}"}

    async def get(self, path: str, **params: Any) -> dict:
        headers = await self._auth_headers()
        resp = await self._http.get(path, headers=headers, params=params)
        self._check(resp)
        return resp.json()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "TastytradeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.is_error:
            raise TastytradeAPIError(resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# Pydantic base — kebab-case alias generator for REST API fields
# ---------------------------------------------------------------------------

def _to_kebab(name: str) -> str:
    return name.replace("_", "-")


class _Base(BaseModel):
    model_config = {
        "populate_by_name": True,
        "alias_generator": _to_kebab,
    }


# ---------------------------------------------------------------------------
# Market models
# ---------------------------------------------------------------------------

class MarketData(_Base):
    symbol: str
    instrument_type: str | None = None
    updated_at: datetime | None = None
    bid: Decimal | None = None
    bid_size: int | None = None
    ask: Decimal | None = None
    ask_size: int | None = None
    last: Decimal | None = None
    mark: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None


class EarningsReport(_Base):
    actual_eps: Decimal | None = None
    consensus_estimate: Decimal | None = None
    time_of_day: str | None = None
    fiscal_quarter_ending: str | None = None
    estimated: bool | None = None
    updated_at: datetime | None = None


class MarketMetricInfo(_Base):
    symbol: str
    implied_volatility_index: Decimal | None = None
    implied_volatility_index_5day_change: Decimal | None = None
    implied_volatility_rank: Decimal | None = None
    tos_implied_volatility_rank: Decimal | None = None
    tw_implied_volatility_rank: Decimal | None = None
    implied_volatility_percentile: Decimal | None = None
    tos_implied_volatility_percentile: Decimal | None = None
    implied_volatility_updated_at: datetime | None = None
    listed_at: date | None = None
    lendability: str | None = None
    borrow_rate: Decimal | None = None
    market_cap: Decimal | None = None
    implied_volatility_30_day: Decimal | None = None
    historical_volatility_30_day: Decimal | None = None
    historical_volatility_60_day: Decimal | None = None
    historical_volatility_90_day: Decimal | None = None
    iv_hv_30_day_difference: Decimal | None = None
    beta: Decimal | None = None
    corr_spy_3month: Decimal | None = None
    dividend_rate_per_share: Decimal | None = None
    annualized_dividend: Decimal | None = None
    dividend_yield: Decimal | None = None
    earnings: EarningsReport | None = None
    liquidity_rating: int | None = None
    liquidity_value: Decimal | None = None
    liquidity_rank: Decimal | None = None
    price_earnings_ratio: Decimal | None = None
    earnings_per_share: Decimal | None = None


class DividendInfo(_Base):
    occurred_date: date
    amount: Decimal


class EarningsInfo(_Base):
    occurred_date: date
    eps: Decimal | None = None


class Equity(_Base):
    symbol: str
    listed_market: str | None = None
    description: str | None = None
    lendability: str | None = None
    borrow_rate: Decimal | None = None
    market_time_instrument_collection: str | None = None
    is_index: bool = False
    is_etf: bool = False
    is_fractional_quantity_eligible: bool = False
    streamer_symbol: str | None = None


class OptionExpiration(_Base):
    underlying_symbol: str
    root_symbol: str | None = None
    option_chain_type: str | None = None
    shares_per_contract: int | None = None
    expiration_type: str | None = None
    expiration_date: date
    days_to_expiration: int | None = None
    settlement_type: str | None = None


class Option(_Base):
    symbol: str
    underlying_symbol: str
    root_symbol: str | None = None
    option_type: str
    expiration_date: date
    strike_price: Decimal
    exercise_style: str | None = None
    shares_per_contract: int | None = None
    streamer_symbol: str | None = None
    days_to_expiration: int | None = None
    is_closing_only: bool = False
    is_index_option: bool = False


class NestedOptionStrike(_Base):
    strike_price: Decimal
    call: Option | str | None = None
    put: Option | str | None = None


class NestedOptionExpiration(_Base):
    underlying_symbol: str | None = None
    root_symbol: str | None = None
    expiration_type: str | None = None
    expiration_date: date
    days_to_expiration: int | None = None
    settlement_type: str | None = None
    strikes: list[NestedOptionStrike] = Field(default_factory=list)


class NestedOptionChain(_Base):
    underlying_symbol: str
    root_symbol: str | None = None
    option_chain_type: str | None = None
    shares_per_contract: int | None = None
    expirations: list[NestedOptionExpiration] = Field(default_factory=list)


class Future(_Base):
    symbol: str
    product_code: str | None = None
    product_description: str | None = None
    expiration_date: date | None = None
    last_trade_date: date | None = None
    next_active_month: bool = False
    is_front_month: bool = False
    streamer_exchange_code: str | None = None
    streamer_symbol: str | None = None
    tick_size: Decimal | None = None
    notional_multiplier: Decimal | None = None


class SymbolData(_Base):
    symbol: str
    description: str | None = None


# ---------------------------------------------------------------------------
# DXLink event models — field order matches FEED_SETUP acceptEventFields order
# ---------------------------------------------------------------------------

_NAN_LIKE: frozenset[str] = frozenset({"NaN", "Infinity", "-Infinity"})


def _to_decimal(v: Any, *, default: Decimal | None = None) -> Decimal | None:
    if v is None or (isinstance(v, str) and v in _NAN_LIKE):
        return default
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return default


def _to_decimal_zero(v: Any) -> Decimal:
    return _to_decimal(v, default=Decimal(0)) or Decimal(0)


class Event(BaseModel):
    model_config = {
        "populate_by_name": True,
        "alias_generator": to_camel,
    }

    EVENT_TYPE: ClassVar[str]

    event_symbol: str = ""
    event_time: int = 0

    @classmethod
    def from_stream(cls, data: list[Any]) -> list["Event"]:
        """Deserialise COMPACT DXLink flat-list into event instances."""
        fields = list(cls.model_fields.keys())
        n = len(fields)
        if not data or n == 0 or len(data) % n != 0:
            return []
        results: list[Event] = []
        for i in range(0, len(data), n):
            chunk = dict(zip(fields, data[i : i + n]))
            try:
                results.append(cls.model_validate(chunk))
            except Exception:
                pass
        return results


class Quote(Event):
    EVENT_TYPE = "Quote"

    sequence: int = 0
    time_nano_part: int = 0
    bid_time: int = 0
    bid_exchange_code: str = ""
    bid_price: Decimal | None = None
    bid_size: Decimal = Decimal(0)
    ask_time: int = 0
    ask_exchange_code: str = ""
    ask_price: Decimal | None = None
    ask_size: Decimal = Decimal(0)

    @field_validator("bid_price", "ask_price", mode="before")
    @classmethod
    def _dec_opt(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)

    @field_validator("bid_size", "ask_size", mode="before")
    @classmethod
    def _dec_zero(cls, v: Any) -> Decimal:
        return _to_decimal_zero(v)


class Trade(Event):
    EVENT_TYPE = "Trade"

    time: int = 0
    time_nano_part: int = 0
    sequence: int = 0
    exchange_code: str = ""
    price: Decimal | None = None
    change: Decimal | None = None
    size: int | None = None
    day_volume: int | None = None
    day_turnover: Decimal | None = None
    tick_direction: str = ""
    extended_trading_hours: bool = False
    day_id: int = 0

    @field_validator("price", "change", "day_turnover", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


class Candle(Event):
    """OHLCV candlestick. Subscribe with period-encoded symbol e.g. SPY{=1d}."""

    EVENT_TYPE = "Candle"

    index: int = 0
    time: int = 0
    sequence: int = 0
    count: int = 0
    open: Decimal = Decimal(0)
    high: Decimal = Decimal(0)
    low: Decimal = Decimal(0)
    close: Decimal = Decimal(0)
    volume: Decimal | None = None
    vwap: Decimal | None = None
    bid_volume: Decimal | None = None
    ask_volume: Decimal | None = None
    imp_volatility: Decimal | None = None
    open_interest: int | None = None

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def _ohlc(cls, v: Any) -> Decimal:
        return _to_decimal_zero(v)

    @field_validator("volume", "vwap", "bid_volume", "ask_volume", "imp_volatility", mode="before")
    @classmethod
    def _dec_opt(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)

    @field_validator("open_interest", mode="before")
    @classmethod
    def _oi(cls, v: Any) -> int | None:
        if v is None or (isinstance(v, str) and v in _NAN_LIKE):
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None


class Greeks(Event):
    EVENT_TYPE = "Greeks"

    index: int = 0
    time: int = 0
    sequence: int = 0
    price: Decimal | None = None
    volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    rho: Decimal | None = None
    vega: Decimal | None = None

    @field_validator("price", "volatility", "delta", "gamma", "theta", "rho", "vega", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


class Summary(Event):
    EVENT_TYPE = "Summary"

    day_id: int = 0
    day_open_price: Decimal | None = None
    day_high_price: Decimal | None = None
    day_low_price: Decimal | None = None
    day_close_price: Decimal | None = None
    day_close_price_type: str = ""
    prev_day_id: int = 0
    prev_day_close_price: Decimal | None = None
    prev_day_close_price_type: str = ""
    prev_day_volume: Decimal | None = None
    open_interest: int | None = None

    @field_validator(
        "day_open_price", "day_high_price", "day_low_price", "day_close_price",
        "prev_day_close_price", "prev_day_volume",
        mode="before",
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


class Profile(Event):
    EVENT_TYPE = "Profile"

    beta: Decimal | None = None
    eps: Decimal | None = None
    div_freq: int | None = None
    exd_div_amount: Decimal | None = None
    exd_div_date: str | None = None
    high_52_week_price: Decimal | None = None
    low_52_week_price: Decimal | None = None
    shares: Decimal | None = None
    free_float: Decimal | None = None
    high_limit_price: Decimal | None = None
    low_limit_price: Decimal | None = None
    halt_status: str = ""
    status_reason: str = ""
    trading_status: str = ""
    short_sale_restriction: bool = False
    description: str = ""

    @field_validator(
        "beta", "eps", "exd_div_amount", "high_52_week_price", "low_52_week_price",
        "shares", "free_float", "high_limit_price", "low_limit_price",
        mode="before",
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


class TheoPrice(Event):
    EVENT_TYPE = "TheoPrice"

    price: Decimal | None = None
    underlying_price: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    dividend: Decimal | None = None
    interest: Decimal | None = None

    @field_validator("price", "underlying_price", "delta", "gamma", "dividend", "interest", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


class TimeAndSale(Event):
    EVENT_TYPE = "TimeAndSale"

    index: int = 0
    time: int = 0
    time_nano_part: int = 0
    sequence: int = 0
    exchange_code: str = ""
    price: Decimal | None = None
    size: int | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    exchange_sale_conditions: str = ""
    is_valid_tick: bool = False
    type: str = ""
    is_eth_trade: bool = False
    side: str = ""

    @field_validator("price", "bid_price", "ask_price", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


class Underlying(Event):
    EVENT_TYPE = "Underlying"

    time: int = 0
    sequence: int = 0
    volatility: Decimal | None = None
    front_volatility: Decimal | None = None
    back_volatility: Decimal | None = None
    call_volume: int | None = None
    put_volume: int | None = None
    put_call_ratio: Decimal | None = None

    @field_validator("volatility", "front_volatility", "back_volatility", "put_call_ratio", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)


EVENT_REGISTRY: dict[str, type[Event]] = {
    cls.EVENT_TYPE: cls  # type: ignore[attr-defined]
    for cls in [Quote, Trade, Candle, Greeks, Summary, Profile, TheoPrice, TimeAndSale, Underlying]
}


# ---------------------------------------------------------------------------
# DXLink candle helpers
# ---------------------------------------------------------------------------

class CandlePeriod:
    """DXLink period strings for candle subscriptions."""
    ONE_MINUTE     = "1m"
    TWO_MINUTE     = "2m"
    THREE_MINUTE   = "3m"
    FIVE_MINUTE    = "5m"
    TEN_MINUTE     = "10m"
    FIFTEEN_MINUTE = "15m"
    THIRTY_MINUTE  = "30m"
    ONE_HOUR       = "1h"
    TWO_HOUR       = "2h"
    FOUR_HOUR      = "4h"
    ONE_DAY        = "1d"
    ONE_WEEK       = "1w"
    ONE_MONTH      = "1mo"


def build_candle_symbol(ticker: str, period: str, regular_hours_only: bool = False) -> str:
    """Build a DXLink candle subscription symbol e.g. SPY{=1d} or SPY{=5m,tho=true}."""
    params = f"={period}"
    if regular_hours_only:
        params += ",tho=true"
    return f"{ticker}{{{params}}}"


def _dxlink_fields(cls: type[Event]) -> list[str]:
    """Return camelCase DXLink field names in model declaration order."""
    return [to_camel(name) for name in cls.model_fields]


# ---------------------------------------------------------------------------
# DXLink Streamer
# ---------------------------------------------------------------------------

class _Closed:
    """Placed in queues when the reader exits so generators unblock cleanly."""
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc


class DXLinkStreamer:
    """
    Async context manager that manages a DXLink WebSocket session.

    Protocol flow:
      1. GET /api-quote-tokens  →  {token, dxlink-url}
      2. Connect WebSocket
      3. SETUP handshake + AUTH
      4. Per event type: CHANNEL_REQUEST → FEED_SETUP → FEED_SUBSCRIPTION
      5. Receive FEED_DATA (COMPACT format)
      6. KEEPALIVE every 30 s on channel 0
    """

    def __init__(self, client: TastytradeClient) -> None:
        self._client = client
        self._ws: Any = None
        self._dxlink_url: str = ""
        self._dxlink_token: str = ""
        self._authorized = asyncio.Event()
        self._auth_error: Exception | None = None
        self._channel_seq = 1
        self._channel_map: dict[str, int] = {}
        self._channel_opened: dict[int, asyncio.Event] = {}
        self._feed_config_events: dict[int, asyncio.Event] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None

    async def __aenter__(self) -> "DXLinkStreamer":
        await self._connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def subscribe(self, event_cls: type[Event], symbols: list[str]) -> None:
        """Subscribe to an event type for a list of symbols."""
        channel = await self._ensure_channel(event_cls)
        subs = [{"type": event_cls.EVENT_TYPE, "symbol": s} for s in symbols]
        await self._send({"type": "FEED_SUBSCRIPTION", "channel": channel, "add": subs})

    async def subscribe_candles(
        self,
        symbols: list[str],
        period: str = CandlePeriod.ONE_DAY,
        from_time_ms: int | None = None,
        regular_hours_only: bool = False,
    ) -> None:
        """Subscribe to OHLCV candles. from_time_ms defaults to full available history."""
        channel = await self._ensure_channel(Candle)
        if from_time_ms is None:
            from_time_ms = int(1e9)  # ~2001-09-09 — maximum history
        subs = [
            {
                "type": Candle.EVENT_TYPE,
                "symbol": build_candle_symbol(t, period, regular_hours_only),
                "fromTime": from_time_ms,
            }
            for t in symbols
        ]
        await self._send({"type": "FEED_SUBSCRIPTION", "channel": channel, "add": subs})

    async def stream(
        self,
        event_cls: type[Event],
        symbols: list[str],
        *,
        timeout: float | None = None,
    ) -> AsyncGenerator[Event, None]:
        """Subscribe and yield events. timeout is per-event wait in seconds."""
        await self.subscribe(event_cls, symbols)
        q = self._queues[event_cls.EVENT_TYPE]
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return
            if isinstance(item, _Closed):
                if item.exc:
                    raise item.exc
                return
            yield item  # type: ignore[misc]

    async def stream_candles(
        self,
        symbols: list[str],
        period: str = CandlePeriod.ONE_DAY,
        from_time_ms: int | None = None,
        regular_hours_only: bool = False,
        *,
        timeout: float | None = None,
    ) -> AsyncGenerator[Candle, None]:
        """Subscribe and yield Candle events. timeout is per-event wait in seconds."""
        await self.subscribe_candles(symbols, period, from_time_ms, regular_hours_only)
        q = self._queues[Candle.EVENT_TYPE]
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return
            if isinstance(item, _Closed):
                if item.exc:
                    raise item.exc
                return
            yield item  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Internal — connection lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        resp = await self._client.get("/api-quote-tokens")
        token_data = resp["data"]
        self._dxlink_url = token_data["dxlink-url"]
        self._dxlink_token = token_data["token"]

        # ping_interval=None disables websockets' built-in ping/pong, which
        # conflicts with DXLink's own KEEPALIVE protocol causing TIMEOUT errors.
        self._ws = await ws_connect(
            self._dxlink_url,
            ping_interval=None,
            ping_timeout=None,
        )
        self._reader_task = asyncio.create_task(self._reader(), name="dxlink-reader")
        self._keepalive_task = asyncio.create_task(self._keepalive(), name="dxlink-keepalive")

        await self._send({
            "type": "SETUP",
            "channel": 0,
            "version": _DXLINK_VERSION,
            "keepaliveTimeout": 60,
            "acceptKeepaliveTimeout": 60,
        })
        await asyncio.wait_for(self._authorized.wait(), timeout=15)
        if self._auth_error:
            raise self._auth_error
        await self._send({"type": "KEEPALIVE", "channel": 0})

    async def _close(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()

    # ------------------------------------------------------------------
    # Internal — channel management
    # ------------------------------------------------------------------

    async def _ensure_channel(self, event_cls: type[Event]) -> int:
        """Return an open channel for event_cls, opening one if needed."""
        name = event_cls.EVENT_TYPE
        if name in self._channel_map:
            return self._channel_map[name]

        channel = self._channel_seq
        self._channel_seq += 2
        self._channel_map[name] = channel
        self._queues[name] = asyncio.Queue()

        # Register events before sending to avoid race conditions
        opened = asyncio.Event()
        self._channel_opened[channel] = opened
        feed_configured = asyncio.Event()
        self._feed_config_events[channel] = feed_configured

        await self._send({
            "type": "CHANNEL_REQUEST",
            "channel": channel,
            "service": "FEED",
            "parameters": {"contract": "AUTO"},
        })
        await asyncio.wait_for(opened.wait(), timeout=10)

        await self._send({
            "type": "FEED_SETUP",
            "channel": channel,
            "acceptAggregationPeriod": 0.1,
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": {name: _dxlink_fields(event_cls)},
        })
        await asyncio.wait_for(feed_configured.wait(), timeout=10)
        return channel

    # ------------------------------------------------------------------
    # Internal — reader and message dispatch
    # ------------------------------------------------------------------

    async def _reader(self) -> None:
        reader_exc: Exception | None = None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, list):
                    for m in msg:
                        await self._dispatch(m)
                else:
                    await self._dispatch(msg)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            reader_exc = exc
        finally:
            if not self._authorized.is_set():
                self._auth_error = reader_exc or TastytradeStreamError(
                    "DXLink connection closed before authorization completed"
                )
                self._authorized.set()
            closed = _Closed(reader_exc)
            for q in self._queues.values():
                q.put_nowait(closed)
            for ev in self._channel_opened.values():
                ev.set()
            for ev in self._feed_config_events.values():
                ev.set()

    async def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")

        if kind == "SETUP":
            await self._send({
                "type": "AUTH",
                "channel": 0,
                "token": self._dxlink_token,
            })
        elif kind == "AUTH_STATE":
            if msg.get("state") == "AUTHORIZED":
                self._authorized.set()
        elif kind == "CHANNEL_OPENED":
            ch = msg.get("channel")
            ev = self._channel_opened.get(ch)
            if ev:
                ev.set()
        elif kind == "FEED_CONFIG":
            ch = msg.get("channel")
            ev = self._feed_config_events.get(ch)
            if ev:
                ev.set()
        elif kind == "FEED_DATA":
            self._handle_feed_data(msg.get("channel"), msg.get("data", []))
        elif kind == "KEEPALIVE":
            await self._send({"type": "KEEPALIVE", "channel": 0})
        elif kind == "ERROR":
            raise TastytradeStreamError(f"DXLink error: {msg.get('error', 'UNKNOWN')}")

    def _handle_feed_data(self, channel: int, data: list) -> None:
        event_type_name = next(
            (name for name, ch in self._channel_map.items() if ch == channel), None
        )
        if not event_type_name or not data:
            return
        cls = EVENT_REGISTRY.get(event_type_name)
        if cls is None:
            return
        q = self._queues.get(event_type_name)
        if q is None:
            return
        # COMPACT format: data = ["EventType", [val0, val1, ..., val0, val1, ...]]
        if len(data) < 2 or not isinstance(data[1], list):
            return
        for event in cls.from_stream(data[1]):
            q.put_nowait(event)

    async def _send(self, msg: dict) -> None:
        await self._ws.send(json.dumps(msg))

    async def _keepalive(self) -> None:
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL)
                await self._send({"type": "KEEPALIVE", "channel": 0})
        except (ConnectionClosed, asyncio.CancelledError):
            pass


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def register_tools(mcp: Any) -> None:
    """
    Register all Tastytrade MCP tools on the provided FastMCP instance.

    Reads TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN from the environment.
    Each tool creates an httpx client for its request and closes it on completion.
    The TastytradeAuth instance is shared and caches access tokens across calls.
    """
    auth = TastytradeAuth(
        client_id=os.environ["TT_CLIENT_ID"],
        client_secret=os.environ["TT_CLIENT_SECRET"],
        refresh_token=os.environ["TT_REFRESH_TOKEN"],
    )

    # ---- Market Data -------------------------------------------------------

    @mcp.tool()
    async def tt_get_quote(symbol: str, instrument_type: str) -> dict:
        """
        Get a snapshot quote for a single instrument.

        instrument_type: equity | equity-option | future | future-option | cryptocurrency | index
        """
        async with TastytradeClient(auth) as client:
            resp = await client.get(f"/market-data/{instrument_type}/{symbol}")
        return MarketData.model_validate(resp["data"]).model_dump(mode="json")

    @mcp.tool()
    async def tt_get_quotes_by_type(
        equities: list[str] | None = None,
        equity_options: list[str] | None = None,
        futures: list[str] | None = None,
        future_options: list[str] | None = None,
        cryptocurrencies: list[str] | None = None,
        indices: list[str] | None = None,
    ) -> list[dict]:
        """
        Get snapshot quotes for multiple symbols across instrument types in one request.

        Combined symbol limit: 100. Pass lists only for the types you need.
        """
        type_map = {
            "equity":         equities,
            "equity-option":  equity_options,
            "future":         futures,
            "future-option":  future_options,
            "cryptocurrency": cryptocurrencies,
            "index":          indices,
        }
        params: dict = {}
        for key, syms in type_map.items():
            if syms:
                params[f"symbols[{key}][]"] = syms
        async with TastytradeClient(auth) as client:
            resp = await client.get("/market-data/by-type", **params)
        items = resp.get("data", {}).get("items", [])
        return [MarketData.model_validate(i).model_dump(mode="json") for i in items]

    # ---- Market Metrics ----------------------------------------------------

    @mcp.tool()
    async def tt_get_market_metrics(symbols: list[str]) -> list[dict]:
        """
        Get market metrics for one or more symbols: IV rank, IV percentile,
        historical volatility (30/60/90-day), beta, earnings, dividends, etc.
        """
        async with TastytradeClient(auth) as client:
            resp = await client.get("/market-metrics", symbols=",".join(symbols))
        items = resp.get("data", {}).get("items", [])
        return [MarketMetricInfo.model_validate(i).model_dump(mode="json") for i in items]

    @mcp.tool()
    async def tt_get_dividends(symbol: str) -> list[dict]:
        """Get historical dividend events for a symbol."""
        encoded = symbol.replace("/", "%2F")
        async with TastytradeClient(auth) as client:
            resp = await client.get(
                f"/market-metrics/historic-corporate-events/dividends/{encoded}"
            )
        items = resp.get("data", {}).get("items", [])
        return [DividendInfo.model_validate(i).model_dump(mode="json") for i in items]

    @mcp.tool()
    async def tt_get_earnings(symbol: str, start_date: str | None = None) -> list[dict]:
        """
        Get historical earnings reports for a symbol.

        start_date: ISO date string YYYY-MM-DD to filter results on or after that date.
        """
        encoded = symbol.replace("/", "%2F")
        params: dict = {}
        if start_date:
            params["start-date"] = start_date
        async with TastytradeClient(auth) as client:
            resp = await client.get(
                f"/market-metrics/historic-corporate-events/earnings-reports/{encoded}",
                **params,
            )
        items = resp.get("data", {}).get("items", [])
        return [EarningsInfo.model_validate(i).model_dump(mode="json") for i in items]

    @mcp.tool()
    async def tt_get_risk_free_rate() -> str:
        """Get the current risk-free rate used by tastytrade for margin/options pricing."""
        async with TastytradeClient(auth) as client:
            resp = await client.get("/margin-requirements-public-configuration")
        rate = resp.get("data", {}).get("risk-free-rate", "0")
        return str(Decimal(str(rate)))

    # ---- Instruments -------------------------------------------------------

    @mcp.tool()
    async def tt_get_equity(symbol: str) -> dict:
        """Get a single equity instrument by symbol."""
        async with TastytradeClient(auth) as client:
            resp = await client.get(f"/instruments/equities/{symbol}")
        return Equity.model_validate(resp["data"]).model_dump(mode="json")

    @mcp.tool()
    async def tt_get_equities(
        symbols: list[str],
        lendability: str | None = None,
        is_index: bool | None = None,
        is_etf: bool | None = None,
    ) -> list[dict]:
        """
        Get multiple equity instruments.

        lendability: Easy To Borrow | Locate Required | Preborrow
        """
        params: dict = {"symbol[]": symbols, "per-page": 250, "page-offset": 0}
        if lendability is not None:
            params["lendability"] = lendability
        if is_index is not None:
            params["is-index"] = str(is_index).lower()
        if is_etf is not None:
            params["is-etf"] = str(is_etf).lower()
        async with TastytradeClient(auth) as client:
            resp = await client.get("/instruments/equities", **params)
        items = resp.get("data", {}).get("items", [])
        return [Equity.model_validate(i).model_dump(mode="json") for i in items]

    @mcp.tool()
    async def tt_get_option_chain(underlying_symbol: str) -> dict:
        """
        Get the full equity option chain for an underlying.

        Returns a dict keyed by expiration date (YYYY-MM-DD) mapping to
        a list of option contract dicts.
        """
        symbol = underlying_symbol.replace("/", "%2F")
        async with TastytradeClient(auth) as client:
            resp = await client.get(f"/option-chains/{symbol}")
        items = resp.get("data", {}).get("items", [])
        chain: dict[str, list[dict]] = {}
        for raw in items:
            opt = Option.model_validate(raw)
            chain.setdefault(opt.expiration_date.isoformat(), []).append(
                opt.model_dump(mode="json")
            )
        return chain

    @mcp.tool()
    async def tt_get_nested_option_chain(underlying_symbol: str) -> list[dict]:
        """
        Get the equity option chain in nested format: expirations → strikes → call/put pairs.

        Cleaner for display than tt_get_option_chain; use the flat version for
        direct access to all Option objects.
        """
        symbol = underlying_symbol.replace("/", "%2F")
        async with TastytradeClient(auth) as client:
            resp = await client.get(f"/option-chains/{symbol}/nested")
        items = resp.get("data", {}).get("items", [])
        return [NestedOptionChain.model_validate(i).model_dump(mode="json") for i in items]

    @mcp.tool()
    async def tt_get_futures(
        symbols: list[str] | None = None,
        product_codes: list[str] | None = None,
    ) -> list[dict]:
        """
        Get futures contracts, optionally filtered by symbols or product codes (e.g. ES, NQ).
        """
        params: dict = {"per-page": 250, "page-offset": 0}
        if symbols:
            params["symbol[]"] = symbols
        if product_codes:
            params["product-code[]"] = product_codes
        async with TastytradeClient(auth) as client:
            resp = await client.get("/instruments/futures", **params)
        items = resp.get("data", {}).get("items", [])
        return [Future.model_validate(i).model_dump(mode="json") for i in items]

    @mcp.tool()
    async def tt_get_future_option_chain(underlying_symbol: str) -> dict:
        """
        Get the option chain for a futures underlying.

        Returns a dict keyed by expiration date (YYYY-MM-DD) mapping to
        a list of option contract dicts.
        """
        symbol = underlying_symbol.replace("/", "")
        async with TastytradeClient(auth) as client:
            resp = await client.get(f"/futures-option-chains/{symbol}")
        items = resp.get("data", {}).get("items", [])
        chain: dict[str, list[dict]] = {}
        for raw in items:
            opt = Option.model_validate(raw)
            chain.setdefault(opt.expiration_date.isoformat(), []).append(
                opt.model_dump(mode="json")
            )
        return chain

    @mcp.tool()
    async def tt_symbol_search(query: str) -> list[dict]:
        """Search for symbols matching a query string. Returns symbol and description."""
        encoded = query.replace("/", "%2F")
        async with TastytradeClient(auth) as client:
            resp = await client.get(f"/symbols/search/{encoded}")
        items = resp.get("data", {}).get("items", [])
        return [SymbolData.model_validate(i).model_dump(mode="json") for i in items]

    # ---- DXLink Streaming --------------------------------------------------

    @mcp.tool()
    async def tt_stream_quotes(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to live bid/ask quotes for symbols and collect for duration_seconds.

        Returns all Quote events received during the collection window.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for q in streamer.stream(Quote, symbols, timeout=duration_seconds):
                    results.append(q.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_trades(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to live trade prints for symbols and collect for duration_seconds.

        Returns all Trade events received during the collection window.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for t in streamer.stream(Trade, symbols, timeout=duration_seconds):
                    results.append(t.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_candles(
        symbols: list[str],
        period: str = "1d",
        from_date: str | None = None,
        duration_seconds: float = 10.0,
        regular_hours_only: bool = False,
    ) -> list[dict]:
        """
        Fetch historical + live OHLCV candles via DXLink.

        period: 1m | 2m | 3m | 5m | 10m | 15m | 30m | 1h | 2h | 4h | 1d | 1w | 1mo
        from_date: ISO date YYYY-MM-DD; omit for full available history (~2001).
        duration_seconds: how long to collect after subscribing before returning.
        regular_hours_only: set True to filter extended-hours bars.
        """
        from_time_ms: int | None = None
        if from_date:
            from_time_ms = int(
                datetime.fromisoformat(from_date).timestamp() * 1000
            )
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for c in streamer.stream_candles(
                    symbols, period, from_time_ms, regular_hours_only,
                    timeout=duration_seconds,
                ):
                    results.append(c.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_greeks(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to live Greeks (delta, gamma, theta, vega, rho, IV) for option symbols
        and collect for duration_seconds.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for g in streamer.stream(Greeks, symbols, timeout=duration_seconds):
                    results.append(g.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_summaries(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to Summary events (day OHLC, previous close, open interest) for symbols
        and collect for duration_seconds.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for s in streamer.stream(Summary, symbols, timeout=duration_seconds):
                    results.append(s.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_profiles(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to Profile events (52-week high/low, trading status, halt status, shares)
        for symbols and collect for duration_seconds.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for p in streamer.stream(Profile, symbols, timeout=duration_seconds):
                    results.append(p.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_theo_prices(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to TheoPrice events (theoretical price, underlying price, greeks)
        for option symbols and collect for duration_seconds.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for tp in streamer.stream(TheoPrice, symbols, timeout=duration_seconds):
                    results.append(tp.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_time_and_sales(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to time & sales (tick-level trade data) for symbols
        and collect for duration_seconds.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for ts in streamer.stream(TimeAndSale, symbols, timeout=duration_seconds):
                    results.append(ts.model_dump(mode="json"))
        return results

    @mcp.tool()
    async def tt_stream_underlyings(
        symbols: list[str],
        duration_seconds: float = 5.0,
    ) -> list[dict]:
        """
        Subscribe to Underlying events (implied volatility, put/call volumes, put-call ratio)
        for symbols and collect for duration_seconds.
        """
        results: list[dict] = []
        async with TastytradeClient(auth) as client:
            async with DXLinkStreamer(client) as streamer:
                async for u in streamer.stream(Underlying, symbols, timeout=duration_seconds):
                    results.append(u.model_dump(mode="json"))
        return results
