"""
Integration layer for local vector search.

Provides a unified interface that automatically uses local (sqlite-vec + FTS5)
or PostgreSQL (pgvector + TSVECTOR) based on configuration.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.documents.models.chunk_embedding import ChunkEmbedding
from app.documents.models.document_chunk import DocumentChunk
from app.local_vector.models import (
    LocalChunkEmbedding,
    LocalDocument,
    LocalDocumentChunk,
    insert_embedding,
    search_bm25_chunks,
    search_similar_chunks,
)
from app.local_vector.retrieval import LocalRetrievalService
from app.platform.database.session import get_engine

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetrievedChunkRow:
    """Unified chunk result row for both local and PostgreSQL backends."""

    document_id: uuid.UUID
    chunk_id: uuid.UUID
    filename: str
    content: str
    similarity_score: float
    source_type: str = "text"
    chunk_index: int = 0
    section_header: str | None = None
    page_number: int | None = None
    quality_score: float | None = None


class UnifiedChunksRepository:
    """Repository that uses local or PostgreSQL backend based on config."""

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._local_retrieval: LocalRetrievalService | None = None

    @property
    def use_local_vector(self) -> bool:
        """Check if local vector search should be used."""
        return self.settings.local_vector_enabled and self.settings.database_url.startswith(
            "sqlite"
        )

    def _get_local_retrieval(self) -> LocalRetrievalService:
        if self._local_retrieval is None:
            self._local_retrieval = LocalRetrievalService(self.db, self.settings)
        return self._local_retrieval

    # -------------------------------------------------------------------
    # Chunk management (delegate to appropriate backend)
    # -------------------------------------------------------------------

    def replace_document_chunks(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: list[DocumentChunk],
    ) -> list[DocumentChunk]:
        if self.use_local_vector:
            return self._replace_local_chunks(
                tenant_id=tenant_id, document_id=document_id, chunks=chunks
            )
        return self._replace_pg_chunks(tenant_id=tenant_id, document_id=document_id, chunks=chunks)

    def _replace_local_chunks(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: list[DocumentChunk],
    ) -> list[DocumentChunk]:

        # Delete old local chunks
        self.db.execute(
            LocalDocumentChunk.__table__.delete().where(
                LocalDocumentChunk.tenant_id == tenant_id,
                LocalDocumentChunk.document_id == document_id,
            )
        )

        # Insert new local chunks
        local_chunks = []
        for chunk in chunks:
            local_chunk = LocalDocumentChunk(
                id=chunk.id,
                tenant_id=chunk.tenant_id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                chunk_metadata=chunk.chunk_metadata,
                created_at=chunk.created_at,
                quality_score=chunk.quality_score,
            )
            self.db.add(local_chunk)
            local_chunks.append(local_chunk)

        self.db.flush()

        # Sync FTS5 (trigger handles this automatically)
        return chunks

    def _replace_pg_chunks(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        chunks: list[DocumentChunk],
    ) -> list[DocumentChunk]:
        # Use existing PostgreSQL logic from ChunksRepository
        from app.documents.repositories.chunks import ChunksRepository

        repo = ChunksRepository(self.db)
        return repo.replace_document_chunks(
            tenant_id=tenant_id, document_id=document_id, chunks=chunks
        )

    def replace_chunk_embeddings(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        embeddings: list[ChunkEmbedding],
    ) -> list[ChunkEmbedding]:
        if self.use_local_vector:
            return self._replace_local_embeddings(
                tenant_id=tenant_id, document_id=document_id, embeddings=embeddings
            )
        return self._replace_pg_embeddings(
            tenant_id=tenant_id, document_id=document_id, embeddings=embeddings
        )

    def _replace_local_embeddings(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        embeddings: list[ChunkEmbedding],
    ) -> list[ChunkEmbedding]:

        # Delete old local embeddings
        self.db.execute(
            LocalChunkEmbedding.__table__.delete().where(
                LocalChunkEmbedding.tenant_id == tenant_id,
                LocalChunkEmbedding.document_id == document_id,
            )
        )

        # Insert new local embeddings (metadata only, vectors go to sqlite-vec)
        for embedding in embeddings:
            local_embedding = LocalChunkEmbedding(
                id=embedding.id,
                tenant_id=embedding.tenant_id,
                document_id=embedding.document_id,
                chunk_id=embedding.chunk_id,
                provider=embedding.provider,
                model=embedding.model,
                dimension=(
                    len(embedding.embedding)
                    if embedding.embedding
                    else self.settings.embedding_dimension
                ),
                created_at=embedding.created_at,
            )
            self.db.add(local_embedding)

            # Insert vector into sqlite-vec
            if embedding.embedding:
                insert_embedding(get_engine(), str(embedding.chunk_id), embedding.embedding)

        self.db.flush()
        return embeddings

    def _replace_pg_embeddings(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        embeddings: list[ChunkEmbedding],
    ) -> list[ChunkEmbedding]:
        from app.documents.repositories.chunks import ChunksRepository

        repo = ChunksRepository(self.db)
        return repo.replace_chunk_embeddings(
            tenant_id=tenant_id, document_id=document_id, embeddings=embeddings
        )

    # -------------------------------------------------------------------
    # Search methods (delegate to appropriate backend)
    # -------------------------------------------------------------------

    def search_top_k(
        self,
        *,
        tenant_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        if self.use_local_vector:
            return self._search_local_top_k(
                tenant_id=tenant_id,
                query_embedding=query_embedding,
                top_k=top_k,
                document_ids=document_ids,
                created_at_from=created_at_from,
                created_at_to=created_at_to,
                source_types=source_types,
                min_extraction_coverage=min_extraction_coverage,
                max_extraction_coverage=max_extraction_coverage,
            )
        return self._search_pg_top_k(
            tenant_id=tenant_id,
            query_embedding=query_embedding,
            top_k=top_k,
            document_ids=document_ids,
            created_at_from=created_at_from,
            created_at_to=created_at_to,
            source_types=source_types,
            min_extraction_coverage=min_extraction_coverage,
            max_extraction_coverage=max_extraction_coverage,
        )

    def _search_local_top_k(
        self,
        *,
        tenant_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        from app.platform.database.session import get_engine

        engine = get_engine()

        raw_results = search_similar_chunks(
            engine,
            query_embedding,
            top_k=top_k,
            tenant_id=tenant_id,
            document_ids=document_ids,
        )

        results: list[RetrievedChunkRow] = []
        for row in raw_results:
            distance = float(row["distance"])
            similarity_score = 1.0 / (1.0 + max(distance, 0.0))
            metadata = row["chunk_metadata"] or {}
            estimated_page = metadata.get("page_number", (row["chunk_index"] // 4) + 1)

            results.append(
                RetrievedChunkRow(
                    document_id=uuid.UUID(row["document_id"]),
                    chunk_id=uuid.UUID(row["chunk_id"]),
                    filename=row["filename"],
                    content=row["content"],
                    similarity_score=round(similarity_score, 6),
                    source_type=str(row["chunk_metadata"].get("mode", "text")),
                    chunk_index=int(row["chunk_index"]),
                    section_header=metadata.get("header_1") or metadata.get("header_2"),
                    page_number=estimated_page,
                    quality_score=None,  # Not stored in local metadata yet
                )
            )
        return results

    def _search_pg_top_k(
        self,
        *,
        tenant_id: uuid.UUID,
        query_embedding: list[float],
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        from app.documents.repositories.chunks import ChunksRepository

        repo = ChunksRepository(self.db)
        return repo.search_top_k(
            tenant_id=tenant_id,
            query_embedding=query_embedding,
            top_k=top_k,
            document_ids=document_ids,
            created_at_from=created_at_from,
            created_at_to=created_at_to,
            source_types=source_types,
            min_extraction_coverage=min_extraction_coverage,
            max_extraction_coverage=max_extraction_coverage,
        )

    def search_bm25(
        self,
        *,
        tenant_id: uuid.UUID,
        query: str,
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        if self.use_local_vector:
            return self._search_local_bm25(
                tenant_id=tenant_id,
                query=query,
                top_k=top_k,
                document_ids=document_ids,
                created_at_from=created_at_from,
                created_at_to=created_at_to,
                source_types=source_types,
                min_extraction_coverage=min_extraction_coverage,
                max_extraction_coverage=max_extraction_coverage,
            )
        return self._search_pg_bm25(
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
            document_ids=document_ids,
            created_at_from=created_at_from,
            created_at_to=created_at_to,
            source_types=source_types,
            min_extraction_coverage=min_extraction_coverage,
            max_extraction_coverage=max_extraction_coverage,
        )

    def _search_local_bm25(
        self,
        *,
        tenant_id: uuid.UUID,
        query: str,
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        from app.platform.database.session import get_engine

        engine = get_engine()

        raw_results = search_bm25_chunks(
            engine,
            query,
            top_k=top_k,
            tenant_id=tenant_id,
            document_ids=document_ids,
        )

        results: list[RetrievedChunkRow] = []
        for row in raw_results:
            metadata = row["chunk_metadata"] or {}
            estimated_page = metadata.get("page_number", (row["chunk_index"] // 4) + 1)

            results.append(
                RetrievedChunkRow(
                    document_id=uuid.UUID(row["document_id"]),
                    chunk_id=uuid.UUID(row["chunk_id"]),
                    filename=row["filename"],
                    content=row["content"],
                    similarity_score=float(row["rank_score"]),
                    source_type=str(row["chunk_metadata"].get("mode", "text")),
                    chunk_index=int(row["chunk_index"]),
                    section_header=metadata.get("header_1") or metadata.get("header_2"),
                    page_number=estimated_page,
                    quality_score=None,
                )
            )
        return results

    def _search_pg_bm25(
        self,
        *,
        tenant_id: uuid.UUID,
        query: str,
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        from app.documents.repositories.chunks import ChunksRepository

        repo = ChunksRepository(self.db)
        return repo.search_bm25(
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
            document_ids=document_ids,
            created_at_from=created_at_from,
            created_at_to=created_at_to,
            source_types=source_types,
            min_extraction_coverage=min_extraction_coverage,
            max_extraction_coverage=max_extraction_coverage,
        )

    def search_top_k_global(
        self,
        *,
        query_embedding: list[float],
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        if self.use_local_vector:
            return self._search_local_top_k(
                tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),  # bypass
                query_embedding=query_embedding,
                top_k=top_k,
                document_ids=document_ids,
                created_at_from=created_at_from,
                created_at_to=created_at_to,
                source_types=source_types,
                min_extraction_coverage=min_extraction_coverage,
                max_extraction_coverage=max_extraction_coverage,
            )
        return self._search_pg_top_k(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            query_embedding=query_embedding,
            top_k=top_k,
            document_ids=document_ids,
            created_at_from=created_at_from,
            created_at_to=created_at_to,
            source_types=source_types,
            min_extraction_coverage=min_extraction_coverage,
            max_extraction_coverage=max_extraction_coverage,
        )

    def search_bm25_global(
        self,
        *,
        query: str,
        top_k: int,
        document_ids: list[uuid.UUID] | None,
        created_at_from: datetime | None,
        created_at_to: datetime | None,
        source_types: list[str] | None,
        min_extraction_coverage: float | None,
        max_extraction_coverage: float | None,
    ) -> list[RetrievedChunkRow]:
        if self.use_local_vector:
            return self._search_local_bm25(
                tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                query=query,
                top_k=top_k,
                document_ids=document_ids,
                created_at_from=created_at_from,
                created_at_to=created_at_to,
                source_types=source_types,
                min_extraction_coverage=min_extraction_coverage,
                max_extraction_coverage=max_extraction_coverage,
            )
        return self._search_pg_bm25(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            query=query,
            top_k=top_k,
            document_ids=document_ids,
            created_at_from=created_at_from,
            created_at_to=created_at_to,
            source_types=source_types,
            min_extraction_coverage=min_extraction_coverage,
            max_extraction_coverage=max_extraction_coverage,
        )

    # -------------------------------------------------------------------
    # Other methods (delegate to appropriate backend)
    # -------------------------------------------------------------------

    def get_by_document_id(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DocumentChunk]:
        if self.use_local_vector:
            stmt = (
                select(LocalDocumentChunk)
                .where(
                    LocalDocumentChunk.tenant_id == tenant_id,
                    LocalDocumentChunk.document_id == document_id,
                )
                .order_by(LocalDocumentChunk.chunk_index.asc())
                .limit(limit)
                .offset(offset)
            )
            return list(self.db.scalars(stmt).all())
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).get_by_document_id(
            tenant_id=tenant_id, document_id=document_id, limit=limit, offset=offset
        )

    def get_all_by_document_id(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID
    ) -> list[DocumentChunk]:
        if self.use_local_vector:
            stmt = (
                select(LocalDocumentChunk)
                .where(
                    LocalDocumentChunk.tenant_id == tenant_id,
                    LocalDocumentChunk.document_id == document_id,
                )
                .order_by(LocalDocumentChunk.chunk_index.asc())
            )
            return list(self.db.scalars(stmt).all())
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).get_all_by_document_id(
            tenant_id=tenant_id, document_id=document_id
        )

    def get_embedded_chunk_ids(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID
    ) -> set[uuid.UUID]:
        if self.use_local_vector:
            stmt = select(LocalChunkEmbedding.chunk_id).where(
                LocalChunkEmbedding.tenant_id == tenant_id,
                LocalChunkEmbedding.document_id == document_id,
            )
            return set(self.db.scalars(stmt).all())
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).get_embedded_chunk_ids(
            tenant_id=tenant_id, document_id=document_id
        )

    def insert_chunk_embeddings(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        embeddings: list[ChunkEmbedding],
    ) -> int:
        if self.use_local_vector:
            return self._insert_local_embeddings(
                tenant_id=tenant_id, document_id=document_id, embeddings=embeddings
            )
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).insert_chunk_embeddings(
            tenant_id=tenant_id, document_id=document_id, embeddings=embeddings
        )

    def _insert_local_embeddings(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        embeddings: list[ChunkEmbedding],
    ) -> int:
        from app.system.services.storage_quota import (
            StorageQuotaService,
        )

        existing = self.get_embedded_chunk_ids(tenant_id=tenant_id, document_id=document_id)
        pending = []
        for embedding in embeddings:
            if embedding.tenant_id != tenant_id or embedding.document_id != document_id:
                raise ValueError("Embedding tenant_id/document_id mismatch")
            if embedding.chunk_id not in existing:
                pending.append(embedding)

        if not pending:
            return 0

        StorageQuotaService(self.db).ensure_capacity(
            tenant_id=tenant_id,
            additional_bytes=sum(len(list(item.embedding or [])) * 4 for item in pending),
            replacing_bytes=0,
        )

        for embedding in pending:
            local_embedding = LocalChunkEmbedding(
                id=embedding.id,
                tenant_id=embedding.tenant_id,
                document_id=embedding.document_id,
                chunk_id=embedding.chunk_id,
                provider=embedding.provider,
                model=embedding.model,
                dimension=(
                    len(embedding.embedding)
                    if embedding.embedding
                    else self.settings.embedding_dimension
                ),
                created_at=embedding.created_at,
            )
            self.db.add(local_embedding)
            if embedding.embedding:
                insert_embedding(get_engine(), str(embedding.chunk_id), embedding.embedding)

        self.db.flush()
        return len(pending)

    def count_by_document_id(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> int:
        if self.use_local_vector:
            stmt = select(
                select(LocalDocumentChunk.id)
                .where(
                    LocalDocumentChunk.tenant_id == tenant_id,
                    LocalDocumentChunk.document_id == document_id,
                )
                .exists()
            ).scalar()
            return int(stmt or 0)
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).count_by_document_id(
            tenant_id=tenant_id, document_id=document_id
        )

    def get_chunks_by_indices(
        self,
        *,
        tenant_id: uuid.UUID,
        document_indices: dict[uuid.UUID, list[int]],
    ) -> list[DocumentChunk]:
        if self.use_local_vector:
            conditions = []
            for doc_id, indices in document_indices.items():
                if not indices:
                    continue
                conditions.append(
                    and_(
                        LocalDocumentChunk.document_id == doc_id,
                        LocalDocumentChunk.chunk_index.in_(indices),
                    )
                )
            if not conditions:
                return []
            stmt = select(LocalDocumentChunk).where(
                LocalDocumentChunk.tenant_id == tenant_id,
                and_(*conditions),
            )
            return list(self.db.scalars(stmt).all())
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).get_chunks_by_indices(
            tenant_id=tenant_id, document_indices=document_indices
        )

    def get_chunks_by_indices_global(
        self,
        *,
        document_indices: dict[uuid.UUID, list[int]],
    ) -> list[DocumentChunk]:
        if self.use_local_vector:
            conditions = []
            for doc_id, indices in document_indices.items():
                if not indices:
                    continue
                conditions.append(
                    and_(
                        LocalDocumentChunk.document_id == doc_id,
                        LocalDocumentChunk.chunk_index.in_(indices),
                    )
                )
            if not conditions:
                return []
            stmt = select(LocalDocumentChunk).where(and_(*conditions))
            return list(self.db.scalars(stmt).all())
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).get_chunks_by_indices_global(
            document_indices=document_indices
        )

    def delete_by_document_ids(
        self,
        *,
        tenant_id: uuid.UUID,
        document_ids: list[uuid.UUID],
    ) -> None:
        if self.use_local_vector:
            if not document_ids:
                return
            self.db.execute(
                LocalChunkEmbedding.__table__.delete().where(
                    LocalChunkEmbedding.tenant_id == tenant_id,
                    LocalChunkEmbedding.document_id.in_(document_ids),
                )
            )
            self.db.execute(
                LocalDocumentChunk.__table__.delete().where(
                    LocalDocumentChunk.tenant_id == tenant_id,
                    LocalDocumentChunk.document_id.in_(document_ids),
                )
            )
            # FTS5 triggers handle cleanup
            return
        from app.documents.repositories.chunks import ChunksRepository

        ChunksRepository(self.db).delete_by_document_ids(
            tenant_id=tenant_id, document_ids=document_ids
        )

    def get_embedding_summary_by_document_id(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> Any | None:
        if self.use_local_vector:
            stmt = (
                select(
                    LocalChunkEmbedding.provider,
                    LocalChunkEmbedding.model,
                    LocalChunkEmbedding.chunk_id.count().label("embedded_chunk_count"),
                )
                .where(
                    LocalChunkEmbedding.tenant_id == tenant_id,
                    LocalChunkEmbedding.document_id == document_id,
                )
                .group_by(LocalChunkEmbedding.provider, LocalChunkEmbedding.model)
                .order_by(LocalChunkEmbedding.chunk_id.count().desc())
                .limit(1)
            )
            row = self.db.execute(stmt).one_or_none()
            if row is None:
                return None
            from app.documents.repositories.chunks import DocumentEmbeddingSummary

            return DocumentEmbeddingSummary(
                provider=str(row.provider),
                model=str(row.model),
                embedded_chunk_count=int(row.embedded_chunk_count or 0),
            )
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).get_embedding_summary_by_document_id(
            tenant_id=tenant_id, document_id=document_id
        )

    def search_document_text(
        self,
        *,
        tenant_id: uuid.UUID,
        document_ids: list[uuid.UUID],
        query: str,
        limit: int = 25,
    ) -> list[RetrievedChunkRow]:
        if self.use_local_vector:
            normalized_query = query.strip()
            if not document_ids or not normalized_query:
                return []
            stmt = (
                select(LocalDocumentChunk, LocalDocument.filename)
                .join(LocalDocument, LocalDocument.id == LocalDocumentChunk.document_id)
                .where(
                    LocalDocumentChunk.tenant_id == tenant_id,
                    LocalDocumentChunk.document_id.in_(document_ids),
                    LocalDocumentChunk.content.like(f"%{normalized_query}%"),
                )
                .order_by(
                    LocalDocumentChunk.document_id.asc(), LocalDocumentChunk.chunk_index.asc()
                )
                .limit(limit)
            )
            rows = self.db.execute(stmt).all()
            results: list[RetrievedChunkRow] = []
            for chunk, filename in rows:
                metadata = chunk.chunk_metadata or {}
                results.append(
                    RetrievedChunkRow(
                        document_id=chunk.document_id,
                        chunk_id=chunk.id,
                        filename=filename,
                        content=chunk.content,
                        similarity_score=1.0,
                        source_type=metadata.get("mode", "text"),
                        section_header=metadata.get("header_1") or metadata.get("header_2"),
                        page_number=metadata.get("page_number", (chunk.chunk_index // 4) + 1),
                        chunk_index=chunk.chunk_index,
                    )
                )
            return results
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).search_document_text(
            tenant_id=tenant_id, document_ids=document_ids, query=query, limit=limit
        )

    def search_document_text_global(
        self,
        *,
        document_ids: list[uuid.UUID],
        query: str,
        limit: int = 25,
    ) -> list[RetrievedChunkRow]:
        if self.use_local_vector:
            return self.search_document_text(
                tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                document_ids=document_ids,
                query=query,
                limit=limit,
            )
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).search_document_text_global(
            document_ids=document_ids, query=query, limit=limit
        )

    def get_chunk_stats_by_document_ids(
        self,
        *,
        tenant_id: uuid.UUID,
        document_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, Any]:
        if self.use_local_vector:
            from app.documents.repositories.chunks import DocumentChunkStats

            if not document_ids:
                return {}
            stmt = (
                select(
                    LocalDocumentChunk.document_id,
                    LocalDocumentChunk.id.count().label("chunk_count"),
                    LocalDocumentChunk.quality_score.avg().label("avg_quality_score"),
                )
                .where(
                    LocalDocumentChunk.tenant_id == tenant_id,
                    LocalDocumentChunk.document_id.in_(document_ids),
                )
                .group_by(LocalDocumentChunk.document_id)
            )
            rows = self.db.execute(stmt).all()
            return {
                row.document_id: DocumentChunkStats(
                    chunk_count=int(row.chunk_count or 0),
                    avg_quality_score=(
                        float(row.avg_quality_score) if row.avg_quality_score is not None else None
                    ),
                )
                for row in rows
            }
        from app.documents.repositories.chunks import ChunksRepository

        return ChunksRepository(self.db).get_chunk_stats_by_document_ids(
            tenant_id=tenant_id, document_ids=document_ids
        )


def get_chunks_repository(db: Session, settings: Settings) -> Any:
    """Factory function to get the appropriate chunks repository."""
    if settings.local_vector_enabled and settings.database_url.startswith("sqlite"):
        return UnifiedChunksRepository(db, settings)
    from app.documents.repositories.chunks import ChunksRepository

    return ChunksRepository(db)
