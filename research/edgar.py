"""
EDGAR REST API client.

Resolves ticker → CIK, finds the most recent filing of a given type
for a given year, and downloads the PDF from the filing index.

Environment variables:
  EDGAR_USER_AGENT  required by SEC, e.g. "MyApp contact@example.com"
"""
from __future__ import annotations

import os

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


async def find_filing(cik: str, form_type: str, year: int) -> dict:
    """
    Return metadata for the most recent filing of form_type filed in year.

    Returns: {accession_number, filing_date, primary_document, form_type}
    """
    url = f"{_DATA_BASE}/submissions/CIK{cik}.json"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

    recent = data.get("filings", {}).get("recent", {})
    forms        = recent.get("form", [])
    dates        = recent.get("filingDate", [])
    accessions   = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    candidates = []
    for form, date, accession, primary_doc in zip(forms, dates, accessions, primary_docs):
        if form.upper() == form_type.upper() and int(date.split("-")[0]) == year:
            candidates.append({
                "accession_number": accession,
                "filing_date":      date,
                "primary_document": primary_doc,
                "form_type":        form,
            })

    if not candidates:
        raise ValueError(
            f"No {form_type} filing found for CIK {cik} in {year}."
        )

    candidates.sort(key=lambda x: x["filing_date"], reverse=True)
    return candidates[0]


async def download_filing_pdf(cik: str, accession_number: str) -> tuple[bytes, str]:
    """
    Download the PDF from a filing's document index.

    Returns (pdf_bytes, filename).
    Raises ValueError if no PDF is found in the filing index.
    """
    acc_no_dashes = accession_number.replace("-", "")
    cik_int = int(cik)
    index_url = (
        f"{_ARCHIVES}/{cik_int}/{acc_no_dashes}/{accession_number}-index.json"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(index_url, headers=_headers(), timeout=15)
        resp.raise_for_status()
        index = resp.json()

    pdf_file = next(
        (doc["name"] for doc in index.get("documents", [])
         if doc.get("name", "").lower().endswith(".pdf")),
        None,
    )
    if not pdf_file:
        raise ValueError(
            f"No PDF found in EDGAR filing index for accession {accession_number}. "
            "This filing may only be available in HTML format."
        )

    pdf_url = f"{_ARCHIVES}/{cik_int}/{acc_no_dashes}/{pdf_file}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(pdf_url, headers=_headers(), timeout=120)
        resp.raise_for_status()

    return resp.content, pdf_file
