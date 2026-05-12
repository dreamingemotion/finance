"""
Async-friendly wrapper around the synchronous PageIndex SDK.

PageIndex's SDK is blocking; all calls are dispatched to a thread pool
so they don't stall the asyncio event loop.

PageIndex uses LiteLLM internally. Point it at OpenRouter by setting:
  OPENAI_API_KEY   → your OPENROUTER_API_KEY value
  OPENAI_BASE_URL  → https://openrouter.ai/api/v1

Environment variables:
  RESEARCH_WORKSPACE  path to workspace directory (default: ./workspace)
"""
from __future__ import annotations

import asyncio
import json
import os
from functools import partial
from pathlib import Path

from pageindex import PageIndexClient as _SyncClient

_client: _SyncClient | None = None


def _workspace() -> Path:
    path = Path(os.environ.get("RESEARCH_WORKSPACE", "./workspace"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_client() -> _SyncClient:
    global _client
    if _client is None:
        _client = _SyncClient(workspace=_workspace())
    return _client


async def _run_sync(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


async def index_document(pdf_path: Path) -> str:
    """
    Index a PDF and return its stable doc_id.

    If a document with the same filename is already in the workspace,
    the existing doc_id is returned immediately without re-indexing.
    """
    client = get_client()
    existing = next(
        (did for did, doc in client.documents.items()
         if doc.get("doc_name") == pdf_path.name),
        None,
    )
    if existing:
        return existing
    return await _run_sync(client.index, pdf_path)


async def get_document(doc_id: str) -> dict:
    client = get_client()
    result = await _run_sync(client.get_document, doc_id)
    if isinstance(result, str):
        result = json.loads(result)
    return result if isinstance(result, dict) else {"raw": result}


async def get_document_structure(doc_id: str) -> list | dict:
    client = get_client()
    result = await _run_sync(client.get_document_structure, doc_id)
    if isinstance(result, str):
        result = json.loads(result)
    return result


async def get_page_content(doc_id: str, pages: str) -> str:
    client = get_client()
    result = await _run_sync(client.get_page_content, doc_id, pages)
    return result if isinstance(result, str) else json.dumps(result)


def list_indexed_documents() -> list[dict]:
    client = get_client()
    return [{"doc_id": did, **doc} for did, doc in client.documents.items()]
