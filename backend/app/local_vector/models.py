"""
SQLite-local vector and full-text search models.

Replaces PostgreSQL pgvector + TSVECTOR with:
- sqlite-vec extension for vector similarity search
- SQLite FTS5 for BM25/keyword search
- Standard SQLite types (no PostgreSQL dialects)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import generate_uuid7_with_fallback
from app.platform.database.base import Base


class LocalDocumentChunk(Base):
    """SQLite-local document chunk with FTS5 full-text search support."""

    __tablename__ = "local_document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "chunk_index",
            name="uq_local_chunks_tenant_document_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=generate_uuid7_with_fallback,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        name="metadata",
        nullable=False,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class LocalChunkEmbedding(Base):
    """SQLite-local chunk embedding using sqlite-vec virtual table.

    The actual vectors are stored in a sqlite-vec virtual table (vec_chunks)
    created via DDL. This model represents the metadata row.
    """

    __tablename__ = "local_chunk_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "chunk_id",
            name="uq_local_embeddings_tenant_chunk",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=generate_uuid7_with_fallback,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_document_chunks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class LocalDocument(Base):
    """SQLite-local document model (simplified for local-first)."""

    __tablename__ = "local_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_local_documents_tenant_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=generate_uuid7_with_fallback,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(Text, nullable=False)
    storage_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    storage_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
    quarantined: Mapped[bool] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
    extraction_coverage_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


# ---------------------------------------------------------------------------
# SQLite-vec and FTS5 DDL (run once at startup)
# ---------------------------------------------------------------------------

VEC_CHUNKS_DDL = """
-- Virtual table for vector similarity search using sqlite-vec
-- Requires: CREATE VIRTUAL TABLE vec_chunks USING vec0(
--     embedding float[384]  -- dimension matches embedding model
-- );
"""

FTS_CHUNKS_DDL = """
-- FTS5 virtual table for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    chunk_id UNINDEXED,
    tenant_id UNINDEXED,
    document_id UNINDEXED,
    content,
    content=local_document_chunks,
    content_rowid=id,
    tokenize="porter unicode61"
);

-- Triggers to keep FTS5 in sync with local_document_chunks
CREATE TRIGGER IF NOT EXISTS fts_chunks_ai AFTER INSERT ON local_document_chunks BEGIN
    INSERT INTO fts_chunks(rowid, chunk_id, tenant_id, document_id, content)
    VALUES (new.id, new.id, new.tenant_id, new.document_id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS fts_chunks_ad AFTER DELETE ON local_document_chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, chunk_id, tenant_id, document_id, content)
    VALUES ('delete', old.id, old.id, old.tenant_id, old.document_id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS fts_chunks_au AFTER UPDATE ON local_document_chunks BEGIN
    INSERT INTO fts_chunks(fts_chunks, rowid, chunk_id, tenant_id, document_id, content)
    VALUES ('delete', old.id, old.id, old.tenant_id, old.document_id, old.content);
    INSERT INTO fts_chunks(rowid, chunk_id, tenant_id, document_id, content)
    VALUES (new.id, new.id, new.tenant_id, new.document_id, new.content);
END;
"""

LOCAL_TABLES_DDL = f"""
-- Ensure uuid extension (SQLite has built-in support via text)
-- Create the vec_chunks virtual table for embeddings
-- Note: dimension must match the embedding model (384 for bge-small-en-v1.5)
{VEC_CHUNKS_DDL}

-- FTS5 for keyword search
{FTS_CHUNKS_DDL}
"""


def create_local_vector_tables(engine) -> None:
    """Create sqlite-vec and FTS5 virtual tables.

    Must be called after engine creation, before any queries.
    """
    from sqlalchemy import text as sa_text

    from app.local_vector.models import LocalChunkEmbedding, LocalDocument, LocalDocumentChunk
    from app.platform.database.base import Base

    # Get the sqlite-vec extension path
    try:
        import sqlite_vec

        vec_extension_path = sqlite_vec.loadable_path()
    except Exception as exc:
        logger.warning(f"Could not import sqlite_vec: {exc}")
        vec_extension_path = None

    # First, create the regular SQLAlchemy tables
    Base.metadata.create_all(
        engine,
        tables=[
            LocalDocument.__table__,
            LocalDocumentChunk.__table__,
            LocalChunkEmbedding.__table__,
        ],
    )

    with engine.connect() as conn:
        # Enable sqlite-vec extension
        if vec_extension_path:
            try:
                conn.execute(sa_text(f"SELECT load_extension('{vec_extension_path}')"))
                conn.commit()
                logger.info(f"Loaded sqlite_vec extension from {vec_extension_path}")
            except Exception as exc:
                logger.warning(
                    f"Could not load sqlite_vec extension from {vec_extension_path}: {exc}"
                )
        else:
            # Try default name
            try:
                conn.execute(sa_text("SELECT load_extension('sqlite_vec')"))
                conn.commit()
                logger.info("Loaded sqlite_vec extension (default name)")
            except Exception as exc:
                logger.warning(f"Could not load sqlite_vec extension: {exc}")

        # Create vec_chunks virtual table for embeddings
        conn.execute(sa_text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                chunk_id TEXT PRIMARY KEY,
                embedding float[384]
            );
        """))
        conn.commit()

        # Create FTS5 virtual table
        conn.execute(sa_text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
                chunk_id UNINDEXED,
                tenant_id UNINDEXED,
                document_id UNINDEXED,
                content,
                content=local_document_chunks,
                content_rowid=id,
                tokenize="porter unicode61"
            );
        """))
        conn.commit()

        # Sync triggers
        conn.execute(sa_text("""
            CREATE TRIGGER IF NOT EXISTS fts_chunks_ai AFTER INSERT ON local_document_chunks BEGIN
                INSERT INTO fts_chunks(rowid, chunk_id, tenant_id, document_id, content)
                VALUES (new.id, new.id, new.tenant_id, new.document_id, new.content);
            END;
        """))
        conn.execute(sa_text("""
            CREATE TRIGGER IF NOT EXISTS fts_chunks_ad AFTER DELETE ON local_document_chunks BEGIN
                INSERT INTO fts_chunks(fts_chunks, rowid, chunk_id, tenant_id, document_id, content)
                VALUES ('delete', old.id, old.id, old.tenant_id, old.document_id, old.content);
            END;
        """))
        conn.execute(sa_text("""
            CREATE TRIGGER IF NOT EXISTS fts_chunks_au AFTER UPDATE ON local_document_chunks BEGIN
                INSERT INTO fts_chunks(fts_chunks, rowid, chunk_id, tenant_id, document_id, content)
                VALUES ('delete', old.id, old.id, old.tenant_id, old.document_id, old.content);
                INSERT INTO fts_chunks(rowid, chunk_id, tenant_id, document_id, content)
                VALUES (new.id, new.id, new.tenant_id, new.document_id, new.content);
            END;
        """))
        conn.commit()

        # Create vec_chunks virtual table for embeddings
        conn.execute(sa_text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                chunk_id TEXT PRIMARY KEY,
                embedding float[384]
            );
        """))
        conn.commit()

        # Create FTS5 virtual table
        conn.execute(sa_text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
                chunk_id UNINDEXED,
                tenant_id UNINDEXED,
                document_id UNINDEXED,
                content,
                content=local_document_chunks,
                content_rowid=id,
                tokenize="porter unicode61"
            );
        """))
        conn.commit()

        # Sync triggers
        conn.execute(sa_text("""
            CREATE TRIGGER IF NOT EXISTS fts_chunks_ai AFTER INSERT ON local_document_chunks BEGIN
                INSERT INTO fts_chunks(rowid, chunk_id, tenant_id, document_id, content)
                VALUES (new.id, new.id, new.tenant_id, new.document_id, new.content);
            END;
        """))
        conn.execute(sa_text("""
            CREATE TRIGGER IF NOT EXISTS fts_chunks_ad AFTER DELETE ON local_document_chunks BEGIN
                INSERT INTO fts_chunks(fts_chunks, rowid, chunk_id, tenant_id, document_id, content)
                VALUES ('delete', old.id, old.id, old.tenant_id, old.document_id, old.content);
            END;
        """))
        conn.execute(sa_text("""
            CREATE TRIGGER IF NOT EXISTS fts_chunks_au AFTER UPDATE ON local_document_chunks BEGIN
                INSERT INTO fts_chunks(fts_chunks, rowid, chunk_id, tenant_id, document_id, content)
                VALUES ('delete', old.id, old.id, old.tenant_id, old.document_id, old.content);
                INSERT INTO fts_chunks(rowid, chunk_id, tenant_id, document_id, content)
                VALUES (new.id, new.id, new.tenant_id, new.document_id, new.content);
            END;
        """))
        conn.commit()


def sync_fts_chunks(engine) -> None:
    """Rebuild FTS5 index from existing chunks (for migrations)."""
    from sqlalchemy import text as sa_text

    with engine.connect() as conn:
        conn.execute(sa_text("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild');"))
        conn.commit()


def insert_embedding(engine, chunk_id: str, embedding: list[float]) -> None:
    """Insert a vector into sqlite-vec virtual table."""
    # Serialize as binary float32 array
    import struct

    from sqlalchemy import text as sa_text

    vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)

    with engine.connect() as conn:
        conn.execute(
            sa_text("INSERT OR REPLACE INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)"),
            [chunk_id, vec_bytes],
        )
        conn.commit()


def search_similar_chunks(
    engine,
    query_embedding: list[float],
    top_k: int,
    tenant_id: uuid.UUID | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Search for similar chunks using sqlite-vec KNN.

    Returns list of dicts with: chunk_id, distance, content, metadata
    """
    import struct

    from sqlalchemy import text as sa_text

    vec_bytes = struct.pack(f"{len(query_embedding)}f", *query_embedding)

    sql = """
        SELECT
            vc.chunk_id,
            vc.embedding,
            vec_distance_cosine(vc.embedding, :query_vec) as distance,
            ldc.content,
            ldc.chunk_metadata,
            ldc.chunk_index,
            ldc.document_id,
            ldc.tenant_id,
            ldoc.filename
        FROM vec_chunks vc
        JOIN local_document_chunks ldc ON ldc.id = vc.chunk_id
        JOIN local_documents ldoc ON ldoc.id = ldc.document_id
        WHERE 1=1
    """
    params = {"query_vec": vec_bytes}

    if tenant_id:
        sql += " AND ldc.tenant_id = :tenant_id"
        params["tenant_id"] = str(tenant_id)

    if document_ids:
        placeholders = ",".join([f":doc_id_{i}" for i in range(len(document_ids))])
        sql += f" AND ldc.document_id IN ({placeholders})"
        for i, doc_id in enumerate(document_ids):
            params[f"doc_id_{i}"] = str(doc_id)

    sql += " ORDER BY distance ASC LIMIT :top_k"
    params["top_k"] = top_k

    with engine.connect() as conn:
        result = conn.execute(sa_text(sql), params).mappings().all()

    return [dict(row) for row in result]


def search_bm25_chunks(
    engine,
    query: str,
    top_k: int,
    tenant_id: uuid.UUID | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Search chunks using FTS5 BM25 ranking."""
    from sqlalchemy import text as sa_text

    # Use FTS5's built-in BM25 ranking
    sql = """
        SELECT
            fts.chunk_id,
            bm25(fts) as rank_score,
            ldc.content,
            ldc.chunk_metadata,
            ldc.chunk_index,
            ldc.document_id,
            ldc.tenant_id,
            ldoc.filename
        FROM fts_chunks fts
        JOIN local_document_chunks ldc ON ldc.id = fts.chunk_id
        JOIN local_documents ldoc ON ldoc.id = ldc.document_id
        WHERE fts.content MATCH :query
    """
    params = {"query": query}

    if tenant_id:
        sql += " AND ldc.tenant_id = :tenant_id"
        params["tenant_id"] = str(tenant_id)

    if document_ids:
        placeholders = ",".join([f":doc_id_{i}" for i in range(len(document_ids))])
        sql += f" AND ldc.document_id IN ({placeholders})"
        for i, doc_id in enumerate(document_ids):
            params[f"doc_id_{i}"] = str(doc_id)

    sql += " ORDER BY rank_score ASC LIMIT :top_k"
    params["top_k"] = top_k

    with engine.connect() as conn:
        result = conn.execute(sa_text(sql), params).mappings().all()

    return [dict(row) for row in result]
