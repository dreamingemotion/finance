"""
Document indexing pipeline: HTML → Markdown → tree → JSON store.

Entry point: index_document(filepath, metadata) → doc_id

SEC forms in FULL_INDEX_FORMS always get full tree indexing.
Short documents (< RAW_TOKEN_LIMIT tokens) are stored as a single node.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from research.html_to_markdown import html_to_markdown
from research.tree_builder import count_tokens, md_to_tree
from research import tree_store

logger = logging.getLogger("finance-research")

FULL_INDEX_FORMS: frozenset[str] = frozenset({
    "10-K", "10-Q", "S-1", "S-3", "S-4", "S-11",
    "20-F", "40-F", "DEF 14A", "DEFA14A",
    "6-K", "F-1", "F-3", "F-4", "ARS",
})

RAW_TOKEN_LIMIT: int = 15_000

_FORM_RE = re.compile(r"^[A-Z0-9.\-]+?_(.+?)_\d{4}[-_]")


def _extract_form_type(filename: str) -> str | None:
    m = _FORM_RE.match(filename)
    return m.group(1) if m else None


def _normalize_form(form: str) -> str:
    form = form.strip()
    if form.endswith("/A"):
        form = form[:-2]
    if form.startswith("Form "):
        form = form[5:]
    return form


def _should_full_index(form: str | None, tokens: int) -> bool:
    if form is None or tokens > RAW_TOKEN_LIMIT:
        return True
    return _normalize_form(form) in FULL_INDEX_FORMS


def _raw_tree(source_name: str, form: str | None, text: str) -> dict:
    """Single-node tree for short/unstructured documents."""
    preview = text[:300].replace("\n", " ").strip()
    if len(text) > 300:
        preview += "..."
    return {
        "doc_name":   source_name,
        "index_mode": "raw",
        "structure": [{
            "node_id": "0000",
            "title":   source_name,
            "summary": preview,
            "text":    text,
        }],
    }


def index_document(filepath: str | Path, metadata: dict | None = None) -> str:
    """
    Index a document and return its doc_id.

    Supports HTML/HTM (converted to Markdown first) and Markdown directly.
    The resulting tree is stored as JSON in the workspace indexes directory.
    If the document is already indexed (same source filename), returns the
    existing doc_id without re-processing.
    """
    filepath    = Path(filepath)
    source_file = filepath.name
    meta        = metadata or {}

    # Return cached doc_id if already indexed
    existing = tree_store.find_by_source(source_file)
    if existing:
        logger.info(f"Already indexed: {source_file} → {existing}")
        return existing

    suffix = filepath.suffix.lower()

    if suffix in (".html", ".htm"):
        markdown_str = html_to_markdown(filepath)
        form_type    = meta.get("form_type") or _extract_form_type(source_file)
        if form_type:
            meta["form_type"] = form_type

        tokens = count_tokens(markdown_str)

        if not _should_full_index(form_type, tokens):
            logger.info(f"Raw-storing {source_file} ({form_type}, {tokens} tokens)")
            tree_data = _raw_tree(filepath.stem, form_type, markdown_str)
        else:
            tmp_dir = tempfile.mkdtemp()
            tmp_md  = Path(tmp_dir) / f"{filepath.stem}.md"
            tmp_md.write_text(markdown_str, encoding="utf-8")
            try:
                tree_data = md_to_tree(tmp_md)
            finally:
                tmp_md.unlink(missing_ok=True)
                try:
                    os.rmdir(tmp_dir)
                except OSError:
                    pass

    elif suffix in (".md", ".markdown"):
        tree_data = md_to_tree(filepath)

    else:
        raise ValueError(f"Unsupported file type: {suffix}. Supported: .html, .htm, .md")

    return tree_store.save_tree(source_file, tree_data, meta)
