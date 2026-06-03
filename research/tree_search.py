"""
PageIndex-style retrieval over stored document trees.

Strategy:
  1. Keyword search (fast, always runs)
  2. If keyword scores are weak, invoke LLM reasoning over the tree structure
  3. Return best results with section citations

Adapted from the PageIndex RAG tree_search implementation.
"""
from __future__ import annotations

import json
import logging
import os
import re

from openai import AsyncOpenAI

from research import tree_store
from research.tree_builder import structure_to_list

logger = logging.getLogger("finance-research")


_NOISE_TITLES = frozenset({
    "table of contents", "index", "signatures", "power of attorney",
    "exhibit index", "certifications",
})

_SEARCH_SYSTEM = """\
You are an expert document navigation assistant for SEC filings.

Given a document's section tree and a user query, identify the node_ids
of the sections most likely to contain the answer.

Return ONLY a comma-separated list of node_ids (e.g. "0005, 0012, 0023").
Select at most {max_results} nodes, most relevant first.
If no sections seem relevant, return NONE."""


def _openrouter() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )


def _is_noise(node: dict) -> bool:
    return node.get("title", "").strip().lower() in _NOISE_TITLES


def _flatten(structure: list | dict, doc_id: str = "", doc_name: str = "") -> list[dict]:
    """Flatten tree into a list of searchable node dicts."""
    nodes = structure_to_list(structure)
    return [
        {
            "doc_id":   doc_id,
            "doc_name": doc_name,
            "node_id":  n.get("node_id", ""),
            "title":    n.get("title", ""),
            "summary":  n.get("summary", ""),
            "text":     n.get("text", ""),
        }
        for n in nodes
    ]


def _score(node: dict, terms: list[str]) -> int:
    score = 0
    title   = node["title"].lower()
    summary = node["summary"].lower()
    text    = (node["text"] or "").lower()
    for term in terms:
        t = term.lower()
        if t in title:   score += 5
        if t in summary: score += 3
        if t in text:    score += 1
    return score


def _snippet(text: str, terms: list[str]) -> str:
    for term in terms:
        idx = text.lower().find(term.lower())
        if idx >= 0:
            s = max(0, idx - 100)
            e = min(len(text), idx + 300)
            return ("..." if s else "") + text[s:e] + ("..." if e < len(text) else "")
    return text[:400] + ("..." if len(text) > 400 else "")


def _keyword_search(all_nodes: list[dict], terms: list[str], max_results: int) -> list[dict]:
    scored = []
    for node in all_nodes:
        if _is_noise(node):
            continue
        s = _score(node, terms)
        if s > 0:
            scored.append({**node, "score": s, "text_snippet": _snippet(node["text"], terms)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max_results]


def _tree_overview(structure: list | dict, max_depth: int = 3, depth: int = 0) -> str:
    lines = []
    if isinstance(structure, dict):
        structure = [structure]
    if not isinstance(structure, list):
        return ""
    for node in structure:
        if _is_noise(node):
            continue
        node_id = node.get("node_id", "")
        title   = node.get("title", "")
        summary = node.get("summary", "")
        indent  = "  " * depth
        line    = f"{indent}[{node_id}] {title}"
        if summary and len(summary) > 20:
            short = summary[:200].replace("\n", " ")
            if len(summary) > 200:
                short += "..."
            line += f" — {short}"
        lines.append(line)
        if depth < max_depth and node.get("nodes"):
            lines.append(_tree_overview(node["nodes"], max_depth, depth + 1))
    return "\n".join(lines)


async def _reasoning_search(
    query: str,
    record: dict,
    max_results: int = 5,
) -> list[dict]:
    """Use LLM to navigate tree structure and select relevant node_ids."""
    tree      = record.get("tree", {})
    structure = tree.get("structure", [])
    doc_id    = record.get("doc_id", "")
    doc_name  = tree.get("doc_name", record.get("source_file", ""))

    if not structure:
        return []

    overview = _tree_overview(structure, max_depth=3)
    system   = _SEARCH_SYSTEM.format(max_results=max_results)
    prompt   = (
        f'USER QUERY: "{query}"\n\n'
        f"DOCUMENT TREE:\n{overview}\n\n"
        "RELEVANT NODE_IDS:"
    )

    try:
        client = _openrouter()
        model  = os.environ.get(
            "REASONING_MODEL",
            os.environ.get("GENERATION_MODEL", "anthropic/claude-haiku-4.5"),
        )
        resp   = await client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        if not raw or raw.upper() == "NONE":
            return []

        selected_ids = [nid.strip() for nid in raw.split(",") if nid.strip()]
        all_nodes    = _flatten(structure, doc_id=doc_id, doc_name=doc_name)
        node_map     = {n["node_id"]: n for n in all_nodes}

        results = []
        for nid in selected_ids[:max_results]:
            if nid in node_map:
                n = node_map[nid]
                text = n["text"] or ""
                results.append({
                    **n,
                    "score":          10,
                    "text_snippet":   text[:400] + ("..." if len(text) > 400 else ""),
                    "selected_by":    "llm_reasoning",
                    "reasoning_model": model,
                })

        logger.info(f"LLM reasoning selected {len(results)} nodes for: {query[:60]}")
        return results

    except Exception as exc:
        logger.warning(f"LLM reasoning failed: {exc}")
        return []


async def search_trees(
    query: str,
    max_results: int = 10,
    doc_id: str | None = None,
    use_reasoning: bool = True,
) -> list[dict]:
    """
    Search across indexed trees using LLM relevancy scoring with keyword fallback.

    If doc_id is given, search only that document.
    If use_reasoning=True (default), Haiku always determines relevancy by
    navigating the tree structure. Keyword results are used only if the LLM
    call fails or returns nothing.
    """
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return []

    if doc_id:
        record   = tree_store.load_tree(doc_id)
        records  = [record] if record else []
    else:
        record   = None
        records  = tree_store.load_all_trees()

    all_nodes: list[dict] = []
    for rec in records:
        t    = rec.get("tree", {})
        d_id = rec.get("doc_id", "")
        d_nm = t.get("doc_name", rec.get("source_file", ""))
        all_nodes.extend(_flatten(t.get("structure", []), doc_id=d_id, doc_name=d_nm))

    if use_reasoning and record:
        reasoning_results = await _reasoning_search(query, record, max_results)
        if reasoning_results:
            return reasoning_results[:max_results]

    # Keyword fallback — used when reasoning is disabled or LLM call failed
    return _keyword_search(all_nodes, terms, max_results)

    return keyword_results


def get_document_overview(doc_id: str) -> str:
    """Return a TOC-style listing of all nodes in a document."""
    record = tree_store.load_tree(doc_id)
    if not record:
        return f"Document '{doc_id}' not found."

    tree      = record.get("tree", {})
    doc_name  = tree.get("doc_name", record.get("source_file", ""))
    structure = tree.get("structure", [])
    lines     = [f"Document: {doc_name}", ""]

    def _walk(nodes: list | dict, indent: int = 0) -> None:
        if isinstance(nodes, dict):
            nodes = [nodes]
        if not isinstance(nodes, list):
            return
        for node in nodes:
            prefix  = "  " * indent
            node_id = node.get("node_id", "")
            title   = node.get("title", "Untitled")
            summary = node.get("summary", "")
            line    = f"{prefix}- [{node_id}] {title}"
            if not _is_noise(node) and summary:
                short = summary[:120] + ("..." if len(summary) > 120 else "")
                line += f" — {short}"
            lines.append(line)
            if node.get("nodes"):
                _walk(node["nodes"], indent + 1)

    _walk(structure)
    return "\n".join(lines)


def get_node_by_id(doc_id: str, node_id: str) -> dict | None:
    """Find and return a node by its node_id from a stored document."""
    record = tree_store.load_tree(doc_id)
    if not record:
        return None
    tree      = record.get("tree", {})
    structure = tree.get("structure", [])
    all_nodes = structure_to_list(structure)
    return next((n for n in all_nodes if n.get("node_id") == node_id), None)
