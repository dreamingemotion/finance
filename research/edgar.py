"""
EDGAR REST API client.

Resolves ticker → CIK, finds the most recent filing of a given type
for a given year, and downloads the PDF from the filing index.

Environment variables:
  EDGAR_USER_AGENT  required by SEC, e.g. "MyApp contact@example.com"
"""
from __future__ import annotations

import os
import re

import httpx

_DATA_BASE  = "https://data.sec.gov"
_ARCHIVES   = "https://www.sec.gov/Archives/edgar/data"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _headers() -> dict[str, str]:
    agent = os.environ.get("EDGAR_USER_AGENT", "FinanceResearchMCP contact@example.com")
    return {"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}


async def get_cik(ticker: str) -> str:
    """Return zero-padded 10-digit CIK for a ticker symbol."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(_TICKERS_URL, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry["ticker"].upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)

    raise ValueError(f"Ticker '{ticker}' not found in EDGAR company list")


async def resolve_cik(ticker_or_cik: str) -> str:
    """
    Resolve a ticker symbol or raw CIK to a zero-padded 10-digit CIK.

    Tries ticker lookup first. If that fails and the input is numeric,
    treats it as a CIK directly and verifies it exists on EDGAR.
    """
    try:
        return await get_cik(ticker_or_cik)
    except ValueError:
        pass

    if not ticker_or_cik.strip().lstrip("0").isdigit():
        raise ValueError(
            f"'{ticker_or_cik}' was not found as a ticker and is not a numeric CIK."
        )

    cik = ticker_or_cik.strip().zfill(10)
    url = f"{_DATA_BASE}/submissions/CIK{cik}.json"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=_headers(), timeout=15)
        if resp.status_code == 404:
            raise ValueError(f"CIK '{ticker_or_cik}' not found on EDGAR.")
        resp.raise_for_status()

    return cik


def _candidates_from_block(block: dict | None, form_type: str, year: int | None) -> list[dict]:
    """Extract matching filing candidates from a submissions block."""
    if not block:
        return []
    forms        = block.get("form", []) or []
    dates        = block.get("filingDate", []) or []
    accessions   = block.get("accessionNumber", []) or []
    primary_docs = block.get("primaryDocument", []) or []

    candidates = []
    for form, date, accession, primary_doc in zip(forms, dates, accessions, primary_docs):
        year_match = year is None or int(date.split("-")[0]) == year
        if form.upper() == form_type.upper() and year_match:
            candidates.append({
                "accession_number": accession,
                "filing_date":      date,
                "primary_document": primary_doc,
                "form_type":        form,
            })
    return candidates


async def find_filing(cik: str, form_type: str, year: int | None = None) -> dict:
    """
    Return metadata for the most recent filing of form_type.

    If year is given, restricts to filings in that calendar year.
    If year is None, returns the most recent filing of that type across all years.

    Searches the inline "recent" block first, then follows paginated "files"
    entries if needed (handles companies with large filing histories).

    Returns: {accession_number, filing_date, primary_document, form_type}
    """
    url = f"{_DATA_BASE}/submissions/CIK{cik}.json"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

    filings_obj = data.get("filings") or {}
    recent      = filings_obj.get("recent") or {}
    candidates  = _candidates_from_block(recent, form_type, year)

    # Follow paginated files if not found in the inline block
    if not candidates:
        for file_entry in (filings_obj.get("files") or []):
            file_name = file_entry.get("name", "")
            if not file_name:
                continue
            page_url = f"{_DATA_BASE}/{file_name}"
            async with httpx.AsyncClient() as client:
                page_resp = await client.get(page_url, headers=_headers(), timeout=15)
                if page_resp.status_code != 200:
                    continue
                page_data = page_resp.json()
            candidates.extend(
                _candidates_from_block(page_data, form_type, year)
            )
            if candidates:
                break

    if not candidates:
        suffix = f" in {year}" if year is not None else ""
        raise ValueError(
            f"No {form_type} filing found for CIK {cik}{suffix}."
        )

    candidates.sort(key=lambda x: x["filing_date"], reverse=True)
    return candidates[0]


async def _list_filing_documents(cik: str, accession_number: str) -> list[str]:
    """
    Return all filenames listed in a filing.

    Tries the JSON index endpoint first. Falls back to parsing the HTML
    directory listing, which EDGAR always serves regardless of filing age
    or filer type.
    """
    acc_no_dashes = accession_number.replace("-", "")
    cik_int = int(cik)
    base = f"{_ARCHIVES}/{cik_int}/{acc_no_dashes}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base}/{accession_number}-index.json",
            headers=_headers(), timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return [doc["name"] for doc in data.get("documents", []) if "name" in doc]

    # JSON index absent — fall back to HTML directory listing
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base}/", headers=_headers(), timeout=15)
        resp.raise_for_status()

    # Pull bare filenames from href attributes (no path separators)
    return re.findall(r'href="([^"/]+\.[a-zA-Z]+)"', resp.text)


async def download_filing_html(
    cik: str,
    accession_number: str,
    primary_document: str,
) -> tuple[bytes, str]:
    """
    Download the primary HTML document for a filing.

    Returns (html_bytes, filename).
    Falls back to the filing index to find an HTML file if the primary
    document URL returns a non-200 status.
    """
    acc_no_dashes = accession_number.replace("-", "")
    cik_int       = int(cik)
    base          = f"{_ARCHIVES}/{cik_int}/{acc_no_dashes}"

    # Try the primary document first
    html_url = f"{base}/{primary_document}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(html_url, headers=_headers(), timeout=120)
        if resp.status_code == 200:
            return resp.content, primary_document

    # Fall back: scan the filing index for any .htm/.html file
    filenames = await _list_filing_documents(cik, accession_number)
    html_file = next(
        (f for f in filenames if f.lower().endswith((".html", ".htm"))),
        None,
    )
    if not html_file:
        raise ValueError(
            f"No HTML document found in EDGAR filing for accession {accession_number}."
        )

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base}/{html_file}", headers=_headers(), timeout=120)
        resp.raise_for_status()

    return resp.content, html_file
