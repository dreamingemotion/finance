"""
SEC EDGAR filing tools.

Six tools for fetching, indexing, and searching SEC filings using
EDGAR's REST API and a local structure-first RAG pipeline.

Pipeline: EDGAR HTML → html_to_markdown → md_to_tree → JSON store
Search:   keyword scoring with LLM reasoning fallback
"""
from __future__ import annotations

import asyncio
import os
import traceback
from pathlib import Path

from research import edgar
from research import indexer
from research import tree_store
from research import tree_search


def _workspace() -> Path:
    path = Path(os.environ.get("RESEARCH_WORKSPACE", "./workspace"))
    path.mkdir(parents=True, exist_ok=True)
    return path


async def submit_filing(ticker: str, form_type: str, year: int) -> dict:
    """
    Fetch a filing from EDGAR and index it for search.

    Downloads the primary HTML document for the most recent filing of
    form_type (e.g. "10-K") filed in the given year, converts it to a
    hierarchical tree, and stores it locally.

    ticker can be a ticker symbol (e.g. "BLK") or a numeric CIK.
    If the ticker lookup fails, the value is tried as a raw CIK.

    Documents are cached — calling this again for the same filing returns
    the existing doc_id immediately without re-downloading or re-indexing.

    Use the returned doc_id with all other filing tools.
    """
    try:
        return await _submit_filing_inner(ticker, form_type, year)
    except Exception:
        return {"error": traceback.format_exc()}


async def _submit_filing_inner(ticker: str, form_type: str, year: int) -> dict:
    cik    = await edgar.resolve_cik(ticker)
    filing = await edgar.find_filing(cik, form_type, year)

    html_name = (
        f"{ticker.upper()}_{form_type.upper()}_{year}_{filing['filing_date']}.html"
    )
    html_path = _workspace() / html_name

    # Check if already indexed before downloading
    existing_doc_id = tree_store.find_by_source(html_name)
    if existing_doc_id:
        doc_listing = next(
            (d for d in tree_store.list_trees() if d["doc_id"] == existing_doc_id),
            {},
        )
        return {
            "doc_id":      existing_doc_id,
            "ticker":      ticker.upper(),
            "form_type":   form_type.upper(),
            "filing_date": filing["filing_date"],
            "node_count":  doc_listing.get("node_count"),
            "cached":      True,
        }

    if not html_path.exists():
        html_bytes, _ = await edgar.download_filing_html(
            cik, filing["accession_number"], filing["primary_document"]
        )
        html_path.write_bytes(html_bytes)

    metadata = {
        "ticker":      ticker.upper(),
        "form_type":   form_type.upper(),
        "filing_date": filing["filing_date"],
        "cik":         cik,
        "accession":   filing["accession_number"],
    }

    doc_id = indexer.index_document(html_path, metadata)

    doc_listing = next(
        (d for d in tree_store.list_trees() if d["doc_id"] == doc_id),
        {},
    )
    return {
        "doc_id":      doc_id,
        "ticker":      ticker.upper(),
        "form_type":   form_type.upper(),
        "filing_date": filing["filing_date"],
        "node_count":  doc_listing.get("node_count"),
        "cached":      False,
    }


async def get_filing_structure(doc_id: str) -> dict:
    """
    Return a full hierarchical table of contents for a filing.

    Each node has title, node_id, summary, and nested children.
    Use node_ids with get_section to retrieve the full text of any section.

    Call this first to orient yourself before fetching specific sections.
    """
    overview = tree_search.get_document_overview(doc_id)
    return {"doc_id": doc_id, "overview": overview}


async def get_section(doc_id: str, node_id: str) -> dict:
    """
    Retrieve the full text of a section by node_id.

    Call get_filing_structure first to find node_ids. Returns section title,
    full text, word count, and summary.
    """
    node = tree_search.get_node_by_id(doc_id, node_id)
    if node is None:
        return {"error": f"Node '{node_id}' not found in document '{doc_id}'"}

    text = node.get("text", "")
    return {
        "doc_id":        doc_id,
        "node_id":       node_id,
        "section_title": node.get("title", ""),
        "summary":       node.get("summary", ""),
        "full_text":     text,
        "word_count":    len(text.split()) if text else 0,
    }


async def search_filing(query: str, doc_id: str) -> dict:
    """
    Search a filing for sections relevant to a query.

    Uses keyword scoring first; falls back to LLM reasoning over the
    document tree when keyword matches are weak. Returns cited passages
    with section titles and node_ids.
    """
    results = await tree_search.search_trees(
        query, max_results=5, doc_id=doc_id, use_reasoning=True
    )
    passages = [
        {
            "text":    r.get("text_snippet", ""),
            "section": r.get("title", ""),
            "node_id": r.get("node_id", ""),
            "score":   r.get("score", 0),
            "doc_id":  r.get("doc_id", doc_id),
        }
        for r in results
    ]
    return {
        "query":   query,
        "doc_id":  doc_id,
        "passages": passages,
    }


async def batch_query(query: str, doc_ids: list[str]) -> dict:
    """
    Search a query across multiple filings simultaneously.

    Runs in parallel. Use this to compare risk factors, disclosures,
    or financials across companies. Results are keyed by doc_id.
    """
    results = await asyncio.gather(
        *[search_filing(query, doc_id) for doc_id in doc_ids],
        return_exceptions=True,
    )
    output = {}
    for doc_id, result in zip(doc_ids, results):
        output[doc_id] = {"error": str(result)} if isinstance(result, Exception) else result
    return {"query": query, "results": output}


async def list_filings() -> list[dict]:
    """List all filings currently indexed in the local workspace."""
    return tree_store.list_trees()


async def delete_filing(doc_id: str) -> dict:
    """
    Permanently delete an indexed filing from the local workspace.

    Does not delete the cached HTML file, only the index.
    """
    deleted = tree_store.delete_tree(doc_id)
    if not deleted:
        return {"error": f"No document with doc_id '{doc_id}'"}
    return {"deleted": True, "doc_id": doc_id}
