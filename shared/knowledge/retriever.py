"""
KnowledgeRetriever — semantic search and category-based retrieval.

Import this in any MCP server that needs to query the knowledge base:

    from shared.knowledge.retriever import KnowledgeRetriever

    async with get_db() as db:
        retriever = KnowledgeRetriever(db)
        results = await retriever.search("VIX buy signal")
"""
from __future__ import annotations

from .embedder import embed_one


class KnowledgeRetriever:
    def __init__(self, db) -> None:
        self._db = db

    async def search(
        self,
        query: str,
        categories: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Semantic search over knowledge chunks.

        Embeds the query and returns the most similar chunks by cosine similarity.
        Optionally restrict to chunks that belong to at least one of the given categories.
        """
        vector = await embed_one(query)
        vec_str = f"[{','.join(str(v) for v in vector)}]"

        if categories:
            rows = await self._db.fetch(
                """
                SELECT
                    c.id,
                    c.document_id,
                    c.content,
                    d.title,
                    d.source_url,
                    (
                        SELECT array_agg(cc.category)
                        FROM knowledge.chunk_categories cc
                        WHERE cc.chunk_id = c.id
                    ) AS categories,
                    1 - (c.embedding <=> $1::vector) AS similarity
                FROM knowledge.chunks c
                JOIN knowledge.documents d ON d.id = c.document_id
                WHERE EXISTS (
                    SELECT 1 FROM knowledge.chunk_categories cc
                    WHERE cc.chunk_id = c.id AND cc.category = ANY($2::text[])
                )
                ORDER BY similarity DESC
                LIMIT $3
                """,
                vec_str, categories, limit,
            )
        else:
            rows = await self._db.fetch(
                """
                SELECT
                    c.id,
                    c.document_id,
                    c.content,
                    d.title,
                    d.source_url,
                    (
                        SELECT array_agg(cc.category)
                        FROM knowledge.chunk_categories cc
                        WHERE cc.chunk_id = c.id
                    ) AS categories,
                    1 - (c.embedding <=> $1::vector) AS similarity
                FROM knowledge.chunks c
                JOIN knowledge.documents d ON d.id = c.document_id
                ORDER BY similarity DESC
                LIMIT $2
                """,
                vec_str, limit,
            )
        return [dict(r) for r in rows]

    async def get_by_category(self, category: str, limit: int = 20) -> list[dict]:
        """Retrieve chunks tagged with a category, most recent first."""
        rows = await self._db.fetch(
            """
            SELECT
                c.id,
                c.document_id,
                c.content,
                d.title,
                d.source_url,
                (
                    SELECT array_agg(cc.category)
                    FROM knowledge.chunk_categories cc
                    WHERE cc.chunk_id = c.id
                ) AS categories,
                c.created_at
            FROM knowledge.chunks c
            JOIN knowledge.documents d ON d.id = c.document_id
            WHERE EXISTS (
                SELECT 1 FROM knowledge.chunk_categories cc
                WHERE cc.chunk_id = c.id AND cc.category = $1
            )
            ORDER BY c.created_at DESC
            LIMIT $2
            """,
            category, limit,
        )
        return [dict(r) for r in rows]

    async def get_document(self, document_id: int) -> dict | None:
        """Retrieve a full document with all its extracted chunks."""
        doc = await self._db.fetchrow(
            "SELECT * FROM knowledge.documents WHERE id = $1",
            document_id,
        )
        if doc is None:
            return None
        chunks = await self._db.fetch(
            """
            SELECT
                c.id,
                c.content,
                (
                    SELECT array_agg(cc.category)
                    FROM knowledge.chunk_categories cc
                    WHERE cc.chunk_id = c.id
                ) AS categories
            FROM knowledge.chunks c
            WHERE c.document_id = $1
            ORDER BY c.id
            """,
            document_id,
        )
        return {**dict(doc), "chunks": [dict(r) for r in chunks]}

    async def list_documents(self) -> list[dict]:
        """List all documents with chunk counts, most recent first."""
        rows = await self._db.fetch(
            """
            SELECT
                d.id,
                d.title,
                d.source_url,
                d.created_at,
                COUNT(c.id) AS chunk_count
            FROM knowledge.documents d
            LEFT JOIN knowledge.chunks c ON c.document_id = d.id
            GROUP BY d.id, d.title, d.source_url, d.created_at
            ORDER BY d.created_at DESC
            """
        )
        return [dict(r) for r in rows]

    async def list_categories(self) -> list[dict]:
        """List all categories (seeded + discovered) with chunk counts."""
        rows = await self._db.fetch(
            """
            SELECT
                cat.name,
                cat.description,
                cat.is_seeded,
                COUNT(cc.chunk_id) AS chunk_count
            FROM knowledge.categories cat
            LEFT JOIN knowledge.chunk_categories cc ON cc.category = cat.name
            GROUP BY cat.name, cat.description, cat.is_seeded
            ORDER BY cat.is_seeded DESC, chunk_count DESC, cat.name
            """
        )
        return [dict(r) for r in rows]
