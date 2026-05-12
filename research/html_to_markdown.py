"""
Convert SEC EDGAR HTML filings to hierarchy-faithful Markdown.

Strategy:
  1. Strip noise: scripts, styles, hidden elements
  2. Map HTML h1-h6 → Markdown # levels
  3. When HTML headings are absent, promote SEC section labels
     ("Part I", "Item 1A.") and bold ALL-CAPS text to ## headings
  4. Convert tables to pipe-delimited Markdown
  5. Clean up artifacts: repeated headers, "(CONTINUED)" markers
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

_HTML_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

_SEC_SECTION_RE = re.compile(
    r"^(?:part|item|section|exhibit|schedule|appendix)"
    r"\s+(?:[ivxlcdmIVXLCDM]+|\d+[a-zA-Z]?)"
    r"(?:\s*[.:\-—].*)?$",
    re.IGNORECASE,
)

_NOISE_TAGS = frozenset({"script", "style", "noscript", "meta", "link", "head", "iframe"})
_INLINE_TAGS = frozenset({"b", "strong", "em", "i", "u", "span", "font", "a", "sup", "sub"})
_BLOCK_TAGS  = frozenset({"p", "div", "section", "article", "main", "aside", "header", "footer", "blockquote"})
_LIST_TAGS   = frozenset({"ul", "ol"})


def _is_hidden(element: Tag) -> bool:
    if not element.attrs:
        return False
    style = element.get("style", "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _is_sec_pattern(text: str) -> bool:
    return bool(_SEC_SECTION_RE.match(text.strip()))


def _is_all_caps_heading(text: str) -> bool:
    text = text.strip()
    if not (4 <= len(text) <= 100):
        return False
    words = text.split()
    if not (1 <= len(words) <= 10):
        return False
    alpha = [c for c in text if c.isalpha()]
    return bool(alpha) and all(c.isupper() for c in alpha)


def _is_bold(element: Tag) -> bool:
    if element.name in ("b", "strong"):
        return True
    if not element.attrs:
        return False
    style = element.get("style", "").lower()
    return "font-weight:bold" in style.replace(" ", "") or "font-weight: bold" in style


def _table_to_markdown(table: Tag) -> str:
    rows = table.find_all("tr")
    if not rows:
        return ""
    lines = []
    for i, row in enumerate(rows):
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        texts = [c.get_text(" ", strip=True).replace("|", "\\|") for c in cells]
        lines.append("| " + " | ".join(texts) + " |")
        if i == 0 and row.find("th"):
            lines.append("| " + " | ".join("---" for _ in texts) + " |")
    return "\n".join(lines)


def _element_to_md(element: Tag | NavigableString, out: list[str], h_level: int) -> None:
    """Recursively convert a BeautifulSoup element into Markdown lines."""
    if isinstance(element, NavigableString):
        text = str(element).strip()
        if text:
            out.append(text)
        return

    if not isinstance(element, Tag):
        return

    tag = (element.name or "").lower()

    if tag in _NOISE_TAGS:
        return
    if _is_hidden(element):
        return

    # Standard HTML headings
    if tag in _HTML_HEADING_LEVELS:
        text = element.get_text(" ", strip=True)
        if text:
            level = _HTML_HEADING_LEVELS[tag]
            out.append(f"\n{'#' * level} {text}\n")
        return

    # Tables
    if tag == "table":
        md = _table_to_markdown(element)
        if md:
            out.append(f"\n{md}\n")
        return

    # Horizontal rule
    if tag == "hr":
        out.append("\n---\n")
        return

    # List items
    if tag == "li":
        text = element.get_text(" ", strip=True)
        if text:
            out.append(f"- {text}")
        return

    if tag in _LIST_TAGS:
        for child in element.children:
            _element_to_md(child, out, h_level)
        return

    # Block-level: check if this element is acting as a heading
    if tag in _BLOCK_TAGS or tag in ("td", "th"):
        # Gather only direct inline/text content (skip nested blocks)
        inline_parts: list[str] = []
        has_nested_block = False
        for child in element.children:
            if isinstance(child, NavigableString):
                t = str(child).strip()
                if t:
                    inline_parts.append(t)
            elif isinstance(child, Tag):
                child_tag = (child.name or "").lower()
                if child_tag in _BLOCK_TAGS or child_tag == "table":
                    has_nested_block = True
                elif child_tag in _INLINE_TAGS:
                    inline_parts.append(child.get_text(" ", strip=True))

        inline_text = " ".join(inline_parts).strip()

        # If the whole element resolves to a heading-like line, emit as heading
        if inline_text and not has_nested_block:
            is_bold_elem = _is_bold(element) or bool(element.find(["b", "strong"]))
            if _is_sec_pattern(inline_text) or (is_bold_elem and _is_all_caps_heading(inline_text)):
                out.append(f"\n{'#' * h_level} {inline_text}\n")
                return

        # Otherwise recurse into children
        sub: list[str] = []
        for child in element.children:
            _element_to_md(child, sub, h_level)
        if sub:
            out.extend(sub)
            out.append("")
        return

    # Inline elements — just return their text via recursion
    for child in element.children:
        _element_to_md(child, out, h_level)


def _clean(text: str) -> str:
    # Strip (CONTINUED) markers
    text = re.sub(r'\(CONTINUED\)', '', text, flags=re.IGNORECASE)

    # Deduplicate headings that appear 3+ times (page-break repetition)
    counts: Counter = Counter()
    kept: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            counts[line] += 1
            if counts[line] > 2:
                continue
        kept.append(line)
    text = "\n".join(kept)

    # Collapse 3+ blank lines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def html_to_markdown(filepath: Path | str) -> str:
    """
    Convert an SEC EDGAR HTML filing to Markdown for tree-based indexing.

    Uses BeautifulSoup to traverse the DOM, maps standard HTML headings
    to Markdown levels, and promotes SEC section labels / bold ALL-CAPS
    text to headings when the document lacks native h1-h6 tags.
    """
    filepath = Path(filepath)
    try:
        raw = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = filepath.read_text(encoding="latin-1")

    soup = BeautifulSoup(raw, "lxml")

    # Remove noise up front
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if isinstance(tag, Tag) and _is_hidden(tag):
            tag.decompose()

    body = soup.find("body") or soup

    # Choose default heading level for SEC pattern / ALL-CAPS detection
    has_html_headings = bool(soup.find(["h2", "h3", "h4", "h5", "h6"]))
    default_h_level = 3 if has_html_headings else 2

    lines: list[str] = []
    for child in body.children:
        _element_to_md(child, lines, default_h_level)

    return _clean("\n".join(lines))
