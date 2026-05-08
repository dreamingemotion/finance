"""
Document ingestion pipeline.

Calls Claude via OpenRouter to extract discrete insight chunks from an article,
then embeds each chunk and stores everything atomically in the knowledge schema.

Environment variables:
  OPENROUTER_API_KEY  your OpenRouter API key
  GENERATION_MODEL    anthropic/claude-sonnet-4-6 (default)
"""
from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

from shared.knowledge.db import SEEDED_CATEGORIES
from shared.knowledge.embedder import embed

_SYSTEM_PROMPT = """\
You are a financial research analyst extracting investment insights from articles.

Extract discrete, self-contained insight units — each a specific claim, data point, \
or actionable observation that stands completely alone without needing the rest of the article.

Rules:
- Each insight must be understandable with no surrounding context
- Preserve specific numbers, percentages, thresholds, and named entities
- One insight per distinct idea; do not bundle unrelated claims
- Assign 1-3 categories per insight from the provided list
- Suggest a new snake_case category name only if truly nothing fits

Respond with a JSON array only — no explanation, no markdown fences:
[{"content": "...", "categories": ["cat1", "cat2"]}]"""


async def _extract_chunks(content: str, title: str) -> list[dict]:
    client = AsyncOpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )
    model = os.environ.get("GENERATION_MODEL", "anthropic/claude-sonnet-4-6")
    category_list = ", ".join(SEEDED_CATEGORIES.keys())

    response = await client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Article title: {title}\n"
                    f"Available categories: {category_list}\n\n"
                    f"{content}"
                ),
            },
        ],
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


async def ingest_document(
    db,
    title: str,
    content: str,
    source_url: str | None = None,
) -> dict:
    """
    Full ingestion pipeline:
      1. Call Claude to extract discrete insight chunks with category assignments
      2. Embed all chunks in a single batched API call
      3. Store document + chunks + categories atomically in a transaction

    Returns a summary: document_id, chunks_stored, new_categories discovered.
    """
    chunks = await _extract_chunks(content, title)
    if not chunks:
        return {"document_id": None, "title": title, "chunks_stored": 0, "new_categories": []}

    vectors = await embed([c["content"] for c in chunks])

    new_categories: set[str] = set()

    async with db.transaction():
        doc_id = await db.fetchval(
            """
            INSERT INTO knowledge.documents (title, source_url, raw_content)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            title, source_url, content,
        )

        for chunk_data, vector in zip(chunks, vectors):
            vec_str = f"[{','.join(str(v) for v in vector)}]"

            chunk_id = await db.fetchval(
                """
                INSERT INTO knowledge.chunks (document_id, content, embedding)
                VALUES ($1, $2, $3::vector)
                RETURNING id
                """,
                doc_id, chunk_data["content"], vec_str,
            )

            for cat in chunk_data.get("categories", []):
                cat = cat.lower().strip().replace(" ", "_")
                await db.execute(
                    """
                    INSERT INTO knowledge.categories (name, is_seeded)
                    VALUES ($1, FALSE)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    cat,
                )
                if cat not in SEEDED_CATEGORIES:
                    new_categories.add(cat)
                await db.execute(
                    "INSERT INTO knowledge.chunk_categories (chunk_id, category) VALUES ($1, $2)",
                    chunk_id, cat,
                )

    return {
        "document_id": doc_id,
        "title": title,
        "chunks_stored": len(chunks),
        "new_categories": sorted(new_categories),
    }
