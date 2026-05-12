"""
JSON file-based storage for indexed document trees.

Each document is stored as a single JSON file:
  {RESEARCH_WORKSPACE}/indexes/{doc_id}.json

  {
      "doc_id":      "BLK_10-K_2024_abcd1234",
      "source_file": "BLK_10-K_2024_2024-02-23.html",
      "metadata":    { "ticker": "BLK", "form_type": "10-K", ... },
      "tree":        { "doc_name": "...", "structure": [...] }
  }
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("finance-research")


def _indexes_dir() -> Path:
    workspace = Path(os.environ.get("RESEARCH_WORKSPACE", "./workspace"))
    d = workspace / "indexes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize(name: str) -> str:
    name = re.sub(r"[^\w\s\-.]", "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:80]


def _make_doc_id(source_file: str) -> str:
    stem = Path(source_file).stem
    h = hashlib.md5(source_file.encode()).hexdigest()[:8]
    return f"{_sanitize(stem)}_{h}"


def save_tree(
    source_file: str,
    tree_data: dict,
    metadata: dict | None = None,
) -> str:
    """Persist a tree to disk and return its doc_id."""
    doc_id = _make_doc_id(source_file)
    record = {
        "doc_id":      doc_id,
        "source_file": source_file,
        "metadata":    metadata or {},
        "tree":        tree_data,
    }
    path = _indexes_dir() / f"{doc_id}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved tree for {source_file} → {doc_id}")
    return doc_id


def load_tree(doc_id: str) -> dict | None:
    """Load a single tree record by doc_id."""
    path = _indexes_dir() / f"{doc_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_trees() -> list[dict]:
    """Return summary info for all indexed documents."""
    results = []
    for path in sorted(_indexes_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tree = data.get("tree", {})
            structure = tree.get("structure", [])
            results.append({
                "doc_id":       data.get("doc_id", path.stem),
                "source_file":  data.get("source_file", ""),
                "doc_name":     tree.get("doc_name", ""),
                "node_count":   _count_nodes(structure),
                "metadata":     data.get("metadata", {}),
            })
        except Exception:
            continue
    return results


def delete_tree(doc_id: str) -> bool:
    """Delete a document by doc_id. Returns True if deleted."""
    path = _indexes_dir() / f"{doc_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def load_all_trees() -> list[dict]:
    """Load all tree records (full data)."""
    results = []
    for path in sorted(_indexes_dir().glob("*.json")):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return results


def find_by_source(source_file: str) -> str | None:
    """Return the doc_id for a source_file if already indexed, else None."""
    doc_id = _make_doc_id(source_file)
    path = _indexes_dir() / f"{doc_id}.json"
    return doc_id if path.exists() else None


def _count_nodes(structure: list | dict) -> int:
    if isinstance(structure, dict):
        return 1 + sum(_count_nodes(c) for c in structure.get("nodes", []))
    elif isinstance(structure, list):
        return sum(_count_nodes(i) for i in structure)
    return 0
