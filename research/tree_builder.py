"""
Build a hierarchical tree from a Markdown document.

Parses Markdown heading levels (# through ######) into a nested tree,
assigns stable node_ids, and generates extractive TF-IDF summaries for
each node — no LLM calls required during indexing.

Adapted from the PageIndex md_to_tree implementation.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

try:
    import tiktoken
    _enc = tiktoken.encoding_for_model("gpt-4o")
except Exception:
    _enc = None


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _enc:
        return len(_enc.encode(text))
    return len(text) // 4  # rough fallback


# ---------------------------------------------------------------------------
# Extractive TF-IDF summarisation (no LLM)
# ---------------------------------------------------------------------------

def extractive_summary(text: str, num_sentences: int = 3) -> str:
    """Return the top N sentences by TF-IDF score, in document order."""
    raw = re.split(r'(?<=[.!?])\s+(?=[A-Z"\(])', text.strip())
    sentences = [s.strip() for s in raw if len(s.strip()) > 40 and not s.strip().startswith("|")]
    if len(sentences) <= num_sentences:
        return " ".join(sentences)

    def tokenize(s: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", s.lower())

    doc_freq: Counter = Counter()
    sentence_tokens: list[list[str]] = []
    for s in sentences:
        tokens = tokenize(s)
        sentence_tokens.append(tokens)
        doc_freq.update(set(tokens))

    n = len(sentences)
    scores: list[float] = []
    for i, tokens in enumerate(sentence_tokens):
        if not tokens:
            scores.append(0.0)
            continue
        tf = Counter(tokens)
        score = sum(
            (tf[w] / len(tokens)) * math.log(n / (1 + doc_freq[w]))
            for w in tf
        )
        if i < 3:
            score *= 1.5
        scores.append(score)

    top = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:num_sentences]
    top.sort()
    return " ".join(sentences[i] for i in top)


def _summarise(text: str) -> str:
    """Return a short summary string for a node."""
    if not text or count_tokens(text) < 30:
        return text[:300] if text else ""
    return extractive_summary(text, num_sentences=3)


# ---------------------------------------------------------------------------
# Markdown → flat node list
# ---------------------------------------------------------------------------

def _extract_nodes_from_markdown(content: str) -> tuple[list[dict], list[str]]:
    """Return (node_list, all_lines) where each node has {node_title, line_num}."""
    header_re = re.compile(r"^(#{1,6})\s+(.+)$")
    code_block_re = re.compile(r"^```")
    nodes: list[dict] = []
    lines = content.split("\n")
    in_code = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if code_block_re.match(stripped):
            in_code = not in_code
            continue
        if not stripped or in_code:
            continue
        m = header_re.match(stripped)
        if m:
            nodes.append({"node_title": m.group(2).strip(), "line_num": i})
    return nodes, lines


def _extract_node_text(node_list: list[dict], lines: list[str]) -> list[dict]:
    """Add {title, line_num, level, text} to each node."""
    header_re = re.compile(r"^(#{1,6})")
    processed = []
    for node in node_list:
        line = lines[node["line_num"] - 1]
        m = header_re.match(line)
        if not m:
            continue
        processed.append({
            "title": node["node_title"],
            "line_num": node["line_num"],
            "level": len(m.group(1)),
        })

    for i, node in enumerate(processed):
        start = node["line_num"] - 1
        end = processed[i + 1]["line_num"] - 1 if i + 1 < len(processed) else len(lines)
        node["text"] = "\n".join(lines[start:end]).strip()

    return processed


# ---------------------------------------------------------------------------
# Tree construction
# ---------------------------------------------------------------------------

def _build_tree(flat_nodes: list[dict]) -> list[dict]:
    """Convert a flat list of levelled nodes into a nested tree."""
    stack: list[tuple[dict, int]] = []
    roots: list[dict] = []

    for node in flat_nodes:
        level = node["level"]
        tree_node: dict = {
            "title": node["title"],
            "node_id": "",
            "text": node["text"],
            "line_num": node["line_num"],
            "nodes": [],
        }
        while stack and stack[-1][1] >= level:
            stack.pop()
        if stack:
            stack[-1][0]["nodes"].append(tree_node)
        else:
            roots.append(tree_node)
        stack.append((tree_node, level))

    return roots


def _assign_ids(data: list | dict, counter: list[int] | None = None) -> None:
    """Assign zero-padded node_ids in pre-order traversal."""
    if counter is None:
        counter = [0]
    if isinstance(data, dict):
        data["node_id"] = str(counter[0]).zfill(4)
        counter[0] += 1
        for child in data.get("nodes", []):
            _assign_ids(child, counter)
    elif isinstance(data, list):
        for item in data:
            _assign_ids(item, counter)


def _add_summaries(data: list | dict) -> None:
    """Add extractive summaries to every node in place."""
    if isinstance(data, dict):
        text = data.get("text", "")
        data["summary"] = _summarise(text)
        for child in data.get("nodes", []):
            _add_summaries(child)
    elif isinstance(data, list):
        for item in data:
            _add_summaries(item)


def _prune_empty(data: list | dict) -> list | dict:
    """Remove empty nodes lists."""
    if isinstance(data, dict):
        data["nodes"] = [_prune_empty(c) for c in data.get("nodes", [])]
        if not data["nodes"]:
            data.pop("nodes", None)
        return data
    elif isinstance(data, list):
        return [_prune_empty(i) for i in data]
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def structure_to_list(structure: list | dict) -> list[dict]:
    """Flatten a nested tree into a list of all nodes."""
    if isinstance(structure, dict):
        result = [structure]
        for child in structure.get("nodes", []):
            result.extend(structure_to_list(child))
        return result
    elif isinstance(structure, list):
        result = []
        for item in structure:
            result.extend(structure_to_list(item))
        return result
    return []


def md_to_tree(md_path: str | Path) -> dict:
    """
    Parse a Markdown file into a hierarchical tree.

    Returns:
        {
            "doc_name": str,
            "structure": [{ title, node_id, text, summary, nodes? }, ...]
        }
    """
    md_path = Path(md_path)
    content = md_path.read_text(encoding="utf-8")

    flat_nodes, all_lines = _extract_nodes_from_markdown(content)
    if not flat_nodes:
        # No headings found — store as a single root node
        return {
            "doc_name": md_path.stem,
            "structure": [{
                "node_id": "0000",
                "title": md_path.stem,
                "text": content,
                "summary": _summarise(content),
            }],
        }

    nodes_with_text = _extract_node_text(flat_nodes, all_lines)
    tree = _build_tree(nodes_with_text)
    _assign_ids(tree)
    _add_summaries(tree)
    tree = _prune_empty(tree)

    return {
        "doc_name": md_path.stem,
        "structure": tree,
    }
