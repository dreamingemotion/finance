"""
Knowledge base query tools (read-only).

These tools search the finance knowledge base that is ingested and managed
by the finance-knowledge MCP server.  They share the same DB and embedder
but never write — use them to surface relevant domain knowledge when
analysing filings, charts, or market data.

Environment variables:
  KNOWLEDGE_DATABASE_URL   postgresql://... (shared with finance-knowledge)
  OPENROUTER_API_KEY       used to embed the search query
  OPENROUTER_BASE_URL      https://openrouter.ai/api/v1 (default)
"""
from __future__ import annotations

from shared.knowledge.db import get_db
from shared.knowledge.retriever import KnowledgeRetriever


async def search_knowledge(
    query: str,
    categories: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """
    Semantic search over the finance knowledge base.

    Embeds the query and returns the most similar knowledge chunks ranked by
    cosine similarity.  Use this to pull in domain knowledge — strategy notes,
    macro context, risk frameworks — before or during analysis.

    categories: optional list of category names to restrict results.
        Common values: risk, market_risk, macro, strategy, technical,
        sentiment, earnings, sector, valuation, options, inference, methodology.
        Call list_knowledge_categories to see all available categories and counts.

    Returns chunks with content, source document title, source_url, categories,
    and similarity score.
    """
    async with get_db() as db:
        retriever = KnowledgeRetriever(db)
        results = await retriever.search(query, categories=categories, limit=limit)
    return {
        "query":              query,
        "categories_filter":  categories,
        "result_count":       len(results),
        "results":            results,
    }


async def list_knowledge_categories() -> list[dict]:
    """
    List all categories in the knowledge base with chunk counts.

    Returns seeded categories (risk, macro, strategy, etc.) and any
    auto-discovered categories.  Use category names with search_knowledge
    to scope queries to a specific domain.
    """
    async with get_db() as db:
        retriever = KnowledgeRetriever(db)
        return await retriever.list_categories()


async def list_knowledge_documents() -> list[dict]:
    """
    List all documents ingested into the knowledge base.

    Returns document id, title, source_url, creation date, and chunk count.
    Use document ids with get_knowledge_document to retrieve full chunk text.
    """
    async with get_db() as db:
        retriever = KnowledgeRetriever(db)
        return await retriever.list_documents()


async def get_knowledge_document(document_id: int) -> dict:
    """
    Retrieve all chunks for a specific knowledge document.

    Returns the document title, source_url, and every chunk with its content
    and categories.  raw_content (the original full text) is omitted to keep
    the response size manageable — use search_knowledge when you want targeted
    passages instead of an entire document.
    """
    async with get_db() as db:
        retriever = KnowledgeRetriever(db)
        doc = await retriever.get_document(document_id)
    if doc is None:
        return {"error": f"Document {document_id} not found"}
    # Strip raw_content — it duplicates the chunks and can be very large
    doc.pop("raw_content", None)
    doc.pop("content_hash", None)
    return doc
