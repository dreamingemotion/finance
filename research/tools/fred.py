"""
FRED (Federal Reserve Economic Data) tool.

Fetches economic time-series from the St. Louis Fed REST API using httpx.
No extra package needed — httpx is already a project dependency.

Requires: FRED_API_KEY environment variable.
Free API key: https://fred.stlouisfed.org/docs/api/api_key.html
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

_BASE_URL = "https://api.stlouisfed.org/fred"

_PERIOD_DAYS: dict[str, int] = {
    "1d":  5,    "3d":   7,   "5d":   10,  "1mo":  35,
    "3mo": 95,   "6mo":  185, "1y":   370,  "2y":   740,
    "5y":  1830, "10y":  3660,
}

_TREASURY_SERIES = [
    {"series_id": "DGS3MO", "maturity": "3M",  "label": "3-Month"},
    {"series_id": "DGS2",   "maturity": "2Y",  "label": "2-Year"},
    {"series_id": "DGS5",   "maturity": "5Y",  "label": "5-Year"},
    {"series_id": "DGS10",  "maturity": "10Y", "label": "10-Year"},
    {"series_id": "DGS30",  "maturity": "30Y", "label": "30-Year"},
]


def _api_key() -> str:
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        raise RuntimeError("FRED_API_KEY environment variable not set")
    return key


def _start_date(period: str) -> str:
    days = _PERIOD_DAYS.get(period, 35)
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


async def _fetch_observations(
    client: httpx.AsyncClient,
    series_id: str,
    observation_start: str,
) -> list[dict]:
    """Fetch and clean observations for one FRED series (strips missing-value dots)."""
    resp = await client.get(
        f"{_BASE_URL}/series/observations",
        params={
            "series_id":         series_id,
            "api_key":           _api_key(),
            "file_type":         "json",
            "observation_start": observation_start,
            "sort_order":        "asc",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    out = []
    for obs in resp.json().get("observations", []):
        v = obs.get("value", ".")
        if v == "." or v is None:
            continue
        try:
            out.append({"date": obs["date"], "value": round(float(v), 4)})
        except (ValueError, TypeError):
            pass
    return out


async def get_fred_series(series_id: str, period: str = "1y") -> dict:
    """
    Fetch a FRED economic time series.

    series_id: FRED series identifier. Common examples:
      Treasury rates : DGS1MO DGS3MO DGS6MO DGS1 DGS2 DGS5 DGS10 DGS20 DGS30
      Fed policy     : DFF (daily fed funds) FEDFUNDS (monthly avg)
      Inflation      : CPIAUCSL (CPI) CPILFESL (core CPI) PCEPI PCEPILFE (core PCE)
      Employment     : UNRATE PAYEMS (nonfarm payrolls) ICSA (initial claims)
      Growth         : GDP GDPC1 (real GDP)
      Spreads        : T10Y2Y (10Y-2Y) T10Y3M (10Y-3M)

    period: look-back window — same convention as get_bars:
      1d 3d 5d 1mo 3mo 6mo 1y 2y 5y 10y

    Returns observations as [{date, value}, ...] oldest-first plus series
    metadata (title, units, frequency). Values are as reported by FRED
    (e.g. percent for rates, index level for CPI). Missing observations
    (weekends/holidays) are stripped — only real data points are returned.
    """
    start = _start_date(period)
    key = _api_key()

    async with httpx.AsyncClient() as client:
        meta_resp, observations = await asyncio.gather(
            client.get(
                f"{_BASE_URL}/series",
                params={"series_id": series_id, "api_key": key, "file_type": "json"},
                timeout=15.0,
            ),
            _fetch_observations(client, series_id, start),
            return_exceptions=True,
        )

    title = units = frequency = None
    if not isinstance(meta_resp, Exception):
        try:
            meta_resp.raise_for_status()
            srs = meta_resp.json().get("seriess", [{}])[0]
            title     = srs.get("title")
            units     = srs.get("units_short")
            frequency = srs.get("frequency_short")
        except Exception:
            pass

    if isinstance(observations, Exception):
        return {"series_id": series_id, "error": str(observations)}

    return {
        "series_id":    series_id,
        "title":        title,
        "units":        units,
        "frequency":    frequency,
        "data_source":  "fred",
        "period":       period,
        "obs_count":    len(observations),
        "observations": observations,
    }


async def get_treasury_yields() -> dict:
    """
    Fetch current US Treasury constant-maturity yields from FRED.

    Fetches DGS3MO, DGS2, DGS5, DGS10, DGS30 in parallel and returns
    each maturity's current yield, previous-day yield, and bps change.
    Data is from the Treasury Department via FRED — authoritative, daily,
    typically published with a 1-business-day lag.

    Returns:
      data_source: "fred"
      as_of:       date of the most recent observation (YYYY-MM-DD)
      yields:      list of { maturity, label, series_id, yield_pct,
                             prev_yield_pct, change_bps, as_of }
    """
    start = _start_date("1mo")  # 35 days — plenty to find two business days
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_fetch_observations(client, s["series_id"], start) for s in _TREASURY_SERIES],
            return_exceptions=True,
        )

    yields: list[dict] = []
    as_of: str | None = None
    for defn, result in zip(_TREASURY_SERIES, results):
        entry: dict = {
            "maturity":  defn["maturity"],
            "label":     defn["label"],
            "series_id": defn["series_id"],
        }
        if isinstance(result, Exception):
            entry.update({
                "error":          str(result),
                "yield_pct":      None,
                "prev_yield_pct": None,
                "change_bps":     None,
            })
        elif not result:
            entry.update({"yield_pct": None, "prev_yield_pct": None, "change_bps": None})
        else:
            curr = result[-1]["value"]
            prev = result[-2]["value"] if len(result) >= 2 else None
            entry["yield_pct"]      = curr
            entry["prev_yield_pct"] = prev
            entry["change_bps"]     = round((curr - prev) * 100, 1) if prev is not None else None
            entry["as_of"]          = result[-1]["date"]
            if as_of is None:
                as_of = result[-1]["date"]
        yields.append(entry)

    return {
        "data_source": "fred",
        "as_of":       as_of,
        "yields":      yields,
    }
