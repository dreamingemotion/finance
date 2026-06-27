"""
Prompt file management for MCP servers.

Each server has a markdown prompt file under prompts/{name}.md relative to
the repo root. These functions load and save those files.

The repo root is resolved as the parent of this file's parent (shared/).
"""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
_PROMPTS_DIR = _REPO_ROOT / "prompts"


def load_prompt(name: str) -> str:
    """Return the contents of prompts/{name}.md."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def save_prompt(name: str, content: str) -> None:
    """Write content to prompts/{name}.md, creating the file if needed."""
    _PROMPTS_DIR.mkdir(exist_ok=True)
    path = _PROMPTS_DIR / f"{name}.md"
    path.write_text(content, encoding="utf-8")
