"""
OpenRouter embedder using the OpenAI-compatible API.

Adapted from the author-agent embedder — same pattern, same model defaults.

Environment variables:
  OPENROUTER_API_KEY   your OpenRouter API key
  OPENROUTER_BASE_URL  https://openrouter.ai/api/v1 (default)
  EMBEDDING_MODEL      openai/text-embedding-3-large (default, 3072 dims)
"""
from __future__ import annotations

import os

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None
_BATCH_SIZE = 96


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
    return _client


async def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts. Returns one vector per text."""
    client = _get_client()
    model = os.environ.get("EMBEDDING_MODEL", "openai/text-embedding-3-large")
    results: list[list[float]] = []
    for i in range(0, len(texts), _BATCH_SIZE):
        batch = texts[i : i + _BATCH_SIZE]
        response = await client.embeddings.create(model=model, input=batch)
        results.extend(item.embedding for item in response.data)
    return results


async def embed_one(text: str) -> list[float]:
    """Embed a single text string."""
    return (await embed([text]))[0]
