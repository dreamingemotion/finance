"""
Document ingestion pipeline.

Two-step flow:
  1. extract_chunks(title, content)               — LLM extraction, returns chunks for review
  2. commit_document(db, title, content, chunks)  — embed + save approved chunks to DB

Extraction uses two parallel passes:
  1. Factual pass  — durable patterns, relationships, and mechanisms (no point-in-time data)
  2. Inference pass — methodologies, causal chains, comparative patterns, open-ended inferences

A cheap classification call determines whether the inference pass is warranted.

Environment variables:
  OPENROUTER_API_KEY    your OpenRouter API key
  GENERATION_MODEL      anthropic/claude-sonnet-4-6 (default)
  CLASSIFICATION_MODEL  google/gemini-3.1-flash-lite (default)
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

Extract discrete, self-contained insight units — each a durable claim, relationship, \
or observation that remains true and useful beyond the article's publication date.

Rules:
- Each insight must be understandable with no surrounding context
- Extract patterns, mechanisms, thresholds, and relationships — not snapshots
- Do NOT extract point-in-time data: current prices, today's rates, this quarter's \
  earnings, recent percentage moves, or any value anchored to a specific date
- Numbers and percentages are acceptable only when they describe a durable threshold \
  or historical pattern (e.g. "credit spreads above 500bps have historically signaled \
  recession"), not a current reading
- Generalise to the functional characteristic, not the named instance — identify \
  the property that makes the insight apply broadly and make that the subject. \
  E.g. "AI" → "sectors perceived to have long-run deflationary potential"; \
  "tech" → "high-multiple growth sectors"; "crypto" → "speculative assets with \
  narrative-driven valuations". Named sectors, companies, or technologies may \
  appear only as parenthetical historical examples, never as the subject of \
  the insight
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
3. Comparative patterns — what the comparisons in the article reveal about \
   relative value, risk, or positioning as a durable tendency, not a current snapshot
4. Non-obvious investable inferences — conclusions not stated directly but \
   logically implied by the argument, expressed as open-ended principles

Rules:
- All insights must be open-ended and applicable beyond the article's publication date
- Do NOT reference specific current prices, rates, valuations, or any value anchored \
  to a specific date — rephrase as conditions or patterns instead
- Do NOT extract point-in-time data: current readings, recent percentage moves, \
  this quarter's results, or named market events tied to a specific moment
- Generalise to the functional characteristic, not the named instance — identify \
  the property that makes the inference apply broadly and make that the subject. \
  E.g. "AI" → "sectors perceived to have long-run deflationary potential"; \
  "tech" → "high-multiple growth sectors"; "crypto" → "speculative assets with \
  narrative-driven valuations". Named sectors, companies, or technologies may \
  appear only as parenthetical historical examples, never as the subject of \
  the insight
- Only extract insights that genuinely exist in the material — if the document is \
  purely factual data (e.g. raw price tables, earnings releases with no commentary) \
  and none of the four types apply, return an empty array
- Each insight must be self-contained and independently understandable
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
    model = os.environ.get("CLASSIFICATION_MODEL", "google/gemini-3.1-flash-lite")

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


async def extract_chunks(title: str, content: str) -> dict:
    """
    Run LLM extraction passes and return chunks for review — does NOT write to DB.

    Each chunk includes a "pass" field ("factual" or "inference") so the reviewer
    can see which pass produced it. Chunks can be edited, removed, or reordered
    before passing to commit_document.

    Returns:
      title, inference_run, factual_count, inference_count, chunks
    """
    run_inference = await _classify_inference(content, title)

    if run_inference:
        factual_chunks, inference_chunks = await asyncio.gather(
            _extract(_FACTUAL_SYSTEM_PROMPT, content, title),
            _extract(_INFERENCE_SYSTEM_PROMPT, content, title),
        )
    else:
        factual_chunks = await _extract(_FACTUAL_SYSTEM_PROMPT, content, title)
        inference_chunks = []

    for c in factual_chunks:
        c["pass"] = "factual"
    for c in inference_chunks:
        c["pass"] = "inference"

    return {
        "title": title,
        "inference_run": run_inference,
        "factual_count": len(factual_chunks),
        "inference_count": len(inference_chunks),
        "chunks": factual_chunks + inference_chunks,
    }


async def commit_document(
    db,
    title: str,
    content: str,
    chunks: list[dict],
    source_url: str | None = None,
    overwrite: bool = False,
) -> dict:
    """
    Embed and save a pre-approved chunk list to the database.

    Accepts chunks from extract_chunks (with optional edits). The "pass" field
    is used for reporting only and stripped before storage. content is required
    for duplicate detection and raw_content storage.
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
                f"To overwrite, call commit_ingest again with overwrite=True."
            ),
        }
    if existing and overwrite:
        await db.execute(
            "DELETE FROM knowledge.documents WHERE id = $1", existing["id"]
        )

    if not chunks:
        return {
            "document_id": None,
            "title": title,
            "chunks_stored": 0,
            "factual_chunks": 0,
            "inference_chunks": 0,
            "new_categories": [],
        }

    factual_count = sum(1 for c in chunks if c.get("pass") == "factual")
    inference_count = sum(1 for c in chunks if c.get("pass") == "inference")

    vectors = await embed([c["content"] for c in chunks])

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
        "factual_chunks": factual_count,
        "inference_chunks": inference_count,
        "new_categories": sorted(new_categories),
    }


async def ingest_document(
    db,
    title: str,
    content: str,
    source_url: str | None = None,
    overwrite: bool = False,
    run_inference: bool | None = None,
) -> dict:
    """
    Single-call ingestion — skips review. Prefer preview_ingest + commit_ingest
    when human approval is desired.
    """
    extracted = await extract_chunks(title, content)
    chunks = extracted["chunks"]
    return await commit_document(db, title, content, chunks, source_url, overwrite)
