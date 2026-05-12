"""
SEC EDGAR filing tools.

Six tools for fetching, indexing, and searching SEC filings using
EDGAR's REST API and PageIndex for structure-aware retrieval.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from openai import AsyncOpenAI

from research import edgar
from research import pageindex_client as pi

_SEARCH_SYSTEM_PROMPT = """\
You are a document navigation assistant for SEC filings.

Given a document tree structure and a search query, identify the most
relevant sections. Return a JSON array only — no explanation, no markdown fences:
[{"node_id": "...", "title": "...", "pages": "5-7"}]

Rules:
- Select 1-5 sections most relevant to the query
- pages format: "5-7" for a range, "5" for a single page
- If no sections are clearly relevant, return []"""


def _openrouter_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )


def _workspace() -> Path:
    path = Path(os.environ.get("RESEARCH_WORKSPACE", "./workspace"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _find_node(tree: list | dict, node_id: str) -> dict | None:
    """Recursively find a node by node_id in a PageIndex tree."""
    if isinstance(tree, dict):
        if tree.get("node_id") == node_id:
            return tree
        for child in tree.get("nodes", []):
            found = _find_node(child, node_id)
            if found:
                return found
    elif isinstance(tree, list):
        for item in tree:
            found = _find_node(item, node_id)
            if found:
                return found
    return None


def _node_pages(node: dict) -> str:
    """Return page range string for a node, e.g. '21-35' or '21'."""
    start = node.get("start_index") or node.get("page_index")
    end   = node.get("end_index")
    if end and end != start:
        return f"{start}-{end}"
    return str(start)


async def submit_filing(ticker: str, form_type: str, year: int) -> dict:
    """
    Fetch a filing from EDGAR and index it with PageIndex.

    Downloads the PDF for the most recent filing of form_type (e.g. "10-K")
    filed in the given year, submits it to PageIndex, and returns a doc_id.

    ticker can be a ticker symbol (e.g. "BLK") or a numeric CIK (e.g. "1364742").
    If the ticker lookup fails, the value is tried as a raw CIK automatically.

    Indexing a large filing (200+ pages) may take several minutes.
    PDFs and indices are cached in the workspace — calling this again for
    the same filing returns the cached doc_id immediately.

    Use the returned doc_id with all other filing tools.
    """
    cik     = await edgar.resolve_cik(ticker)
    filing  = await edgar.find_filing(cik, form_type, year)

    # Stable filename so PageIndex deduplicates by name across calls
    pdf_name = f"{ticker.upper()}_{form_type.upper()}_{year}_{filing['filing_date']}.pdf"
    pdf_path = _workspace() / pdf_name

    if not pdf_path.exists():
        pdf_bytes, _ = await edgar.download_filing_pdf(cik, filing["accession_number"])
        pdf_path.write_bytes(pdf_bytes)

    doc_id   = await pi.index_document(pdf_path)
    doc_meta = await pi.get_document(doc_id)

    return {
        "doc_id":       doc_id,
        "ticker":       ticker.upper(),
        "form_type":    form_type.upper(),
        "filing_date":  filing["filing_date"],
        "page_count":   doc_meta.get("page_count"),
        "status":       doc_meta.get("status", "completed"),
    }


async def get_filing_status(doc_id: str) -> dict:
    """
    Check the processing status of a filing indexed by PageIndex.

    Returns status ("completed", "processing", etc.) and document metadata.
    """
    return await pi.get_document(doc_id)


async def get_filing_structure(doc_id: str) -> dict:
    """
    Return the full hierarchical section tree for a filing.

    Each node contains: title, node_id, page range (start_index/end_index),
    summary, and nested child nodes. Use node_ids with get_section to
    retrieve the full text of any section.

    Use this first to orient yourself in a filing before fetching sections.
    """
    structure = await pi.get_document_structure(doc_id)
    return {"doc_id": doc_id, "structure": structure}


async def get_section(doc_id: str, node_id: str) -> dict:
    """
    Retrieve the full text of a section by node_id.

    Call get_filing_structure first to find node_ids. Returns section title,
    full text content, word count, and page range.
    """
    structure = await pi.get_document_structure(doc_id)
    node = _find_node(structure, node_id)

    if node is None:
        return {"error": f"Node '{node_id}' not found in document '{doc_id}'"}

    pages     = _node_pages(node)
    full_text = await pi.get_page_content(doc_id, pages)

    return {
        "doc_id":        doc_id,
        "node_id":       node_id,
        "section_title": node.get("title", ""),
        "pages":         pages,
        "full_text":     full_text,
        "word_count":    len(full_text.split()) if full_text else 0,
    }


async def search_filing(query: str, doc_id: str) -> dict:
    """
    Search a filing for sections relevant to a query.

    Uses the document's hierarchical structure to identify relevant sections,
    fetches their text, and returns cited passages with section and page info.
    For cross-company search, use batch_query instead.
    """
    structure     = await pi.get_document_structure(doc_id)
    structure_str = json.dumps(structure)

    client = _openrouter_client()
    model  = os.environ.get("GENERATION_MODEL", "anthropic/claude-sonnet-4-6")

    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": _SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": f"Query: {query}\n\nDocument structure:\n{structure_str}"},
        ],
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    relevant_sections = json.loads(raw) if raw and raw != "[]" else []

    passages = []
    for section in relevant_sections:
        pages = section.get("pages", "")
        if not pages:
            continue
        text = await pi.get_page_content(doc_id, pages)
        passages.append({
            "text":    text,
            "section": section.get("title", ""),
            "pages":   pages,
            "node_id": section.get("node_id", ""),
            "doc_id":  doc_id,
        })

    return {
        "query":            query,
        "doc_id":           doc_id,
        "passages":         passages,
        "sections_matched": len(relevant_sections),
    }


async def batch_query(query: str, doc_ids: list[str]) -> dict:
    """
    Search a query across multiple filings simultaneously.

    Runs search_filing in parallel for each doc_id. Use this to compare
    disclosures, risk factors, or financials across companies.
    Returns results keyed by doc_id.
    """
    results = await asyncio.gather(
        *[search_filing(query, doc_id) for doc_id in doc_ids],
        return_exceptions=True,
    )

    output = {}
    for doc_id, result in zip(doc_ids, results):
        output[doc_id] = {"error": str(result)} if isinstance(result, Exception) else result

    return {"query": query, "results": output}
