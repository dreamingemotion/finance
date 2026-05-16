"""
Document ingestion pipeline.

Calls Claude via OpenRouter in two parallel passes:
  1. Factual pass  — discrete claims, data points, and observations
  2. Inference pass — methodologies, causal chains, comparisons, non-obvious inferences

Before the extraction passes, a cheap classification call determines whether the
inference pass is warranted. Both extraction chunks are embedded in a single
batched call and stored atomically under the same document ID.

Environment variables:
  OPENROUTER_API_KEY    your OpenRouter API key
  GENERATION_MODEL      anthropic/claude-sonnet-4-6 (default)
  CLASSIFICATION_MODEL  google/gemini-2.0-flash-001 (default)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

from openai import AsyncOpenAI

from shared.auth.middleware import get_current_user
from shared.knowledge.db import SEEDED_CATEGORIES
from shared.knowledge.embedder import embed

_FACTUAL_SYSTEM_PROMPT = """\
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

_CLASSIFICATION_SYSTEM_PROMPT = """\
Classify whether a document warrants higher-order inference analysis.

Return {"run_inference": true} if the document contains any of:
- Analysis, commentary, or interpretation
- Causal reasoning or if-then logic
- Comparisons between assets, strategies, or time periods
- Forward-looking statements or conclusions

Return {"run_inference": false} if the document is purely factual data with no commentary:
- Raw price or volume tables
- Earnings releases with only numbers and no commentary
- Data exports or structured records with no narrative

Respond with JSON only — no explanation, no markdown fences."""

_INFERENCE_SYSTEM_PROMPT = """\
You are a financial research analyst performing deep inference on articles.

Read the article and extract higher-order insights that go beyond the stated facts. \
Focus on four types:
1. Analytical methodologies — frameworks, models, or approaches used or referenced
2. Causal chains — if-then sequences implied by the analysis \
   (e.g. "rising rates → spread compression → credit selloff")
3. Explicit comparisons — what direct comparisons in the article reveal about \
   relative value, risk, or positioning
4. Non-obvious investable inferences — conclusions not stated directly but \
   logically implied by the data or argument

Rules:
- Only extract insights that genuinely exist in the material — if the document is \
  purely factual data (e.g. raw price tables, earnings releases with no commentary) \
  and none of the four types apply, return an empty array
- Each insight must be self-contained and independently understandable
- Preserve specific numbers, entities, and relationships
- Always include "methodology" for type-1 insights; always include "inference" \
  for types 2-4
- Add 1-2 extra categories from the provided list when clearly relevant
- Do not repeat facts already obvious from the text — synthesize and infer

Respond with a JSON array only — no explanation, no markdown fences:
[{"content": "...", "categories": ["inference"]}]"""


def _make_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )


async def _extract(system_prompt: str, content: str, title: str) -> list[dict]:
    client = _make_client()
    model = os.environ.get("GENERATION_MODEL", "anthropic/claude-sonnet-4-6")
    category_list = ", ".join(SEEDED_CATEGORIES.keys())

    response = await client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": system_prompt},
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


async def _classify_inference(content: str, title: str) -> bool:
    client = _make_client()
    model = os.environ.get("CLASSIFICATION_MODEL", "google/gemini-2.0-flash-001")

    response = await client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": _CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Article title: {title}\n\n{content}"},
        ],
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)["run_inference"]


async def ingest_document(
    db,
    title: str,
    content: str,
    source_url: str | None = None,
    overwrite: bool = False,
    run_inference: bool | None = None,
) -> dict:
    """
    Full ingestion pipeline:
      1. Check for a duplicate by content hash — if found, return duplicate info
         unless overwrite=True, in which case the existing document is deleted first
      2. Run factual and inference extraction passes concurrently
      3. Embed all chunks in a single batched API call
      4. Store document + chunks + categories atomically in a transaction

    Returns a summary: document_id, total chunks_stored, per-pass counts,
    and any new categories discovered.
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    uploaded_by = get_current_user().get("user_id") or None

    existing = await db.fetchrow(
        "SELECT id, title FROM knowledge.documents WHERE content_hash = $1",
        content_hash,
    )
    if existing and not overwrite:
        return {
            "duplicate": True,
            "existing_document_id": existing["id"],
            "existing_title": existing["title"],
            "message": (
                f"This content was already ingested as '{existing['title']}' "
                f"(id={existing['id']}). Ask the user whether to overwrite or cancel. "
                f"To overwrite, call ingest_document again with overwrite=True."
            ),
        }
    if existing and overwrite:
        await db.execute(
            "DELETE FROM knowledge.documents WHERE id = $1", existing["id"]
        )

    if run_inference is None:
        run_inference = await _classify_inference(content, title)

    if run_inference:
        factual_chunks, inference_chunks = await asyncio.gather(
            _extract(_FACTUAL_SYSTEM_PROMPT, content, title),
            _extract(_INFERENCE_SYSTEM_PROMPT, content, title),
        )
    else:
        factual_chunks = await _extract(_FACTUAL_SYSTEM_PROMPT, content, title)
        inference_chunks = []

    all_chunks = factual_chunks + inference_chunks
    if not all_chunks:
        return {
            "document_id": None,
            "title": title,
            "chunks_stored": 0,
            "factual_chunks": 0,
            "inference_chunks": 0,
            "inference_run": run_inference,
            "new_categories": [],
        }

    vectors = await embed([c["content"] for c in all_chunks])

    new_categories: set[str] = set()

    async with db.transaction():
        doc_id = await db.fetchval(
            """
            INSERT INTO knowledge.documents (title, source_url, raw_content, content_hash, uploaded_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            title, source_url, content, content_hash, uploaded_by,
        )

        for chunk_data, vector in zip(all_chunks, vectors):
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
        "chunks_stored": len(all_chunks),
        "factual_chunks": len(factual_chunks),
        "inference_chunks": len(inference_chunks),
        "inference_run": run_inference,
        "new_categories": sorted(new_categories),
    }
