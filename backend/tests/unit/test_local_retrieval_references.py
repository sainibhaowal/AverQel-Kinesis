from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.local_vector.integration import UnifiedChunksRepository
from app.local_vector.models import LocalDocument, LocalDocumentChunk
from app.local_vector.retrieval import LocalRetrievalService

pytestmark = pytest.mark.unit_no_db


def test_local_retrieval_returns_reference_chunks_for_same_tenant() -> None:
    """Local retrieval exposes bibliography chunks with tenant isolation."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    LocalDocument.__table__.create(engine)
    LocalDocumentChunk.__table__.create(engine)

    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    other_document_id = uuid.uuid4()
    with Session(engine) as db:
        db.add_all(
            [
                LocalDocument(
                    id=document_id,
                    tenant_id=tenant_id,
                    filename="sources.pdf",
                    content_type="application/pdf",
                    size_bytes=1,
                    sha256_hash="a" * 64,
                    storage_bucket="local",
                    storage_object_key="sources.pdf",
                ),
                LocalDocument(
                    id=other_document_id,
                    tenant_id=other_tenant_id,
                    filename="private.pdf",
                    content_type="application/pdf",
                    size_bytes=1,
                    sha256_hash="b" * 64,
                    storage_bucket="local",
                    storage_object_key="private.pdf",
                ),
            ]
        )
        db.add_all(
            [
                LocalDocumentChunk(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    document_id=document_id,
                    chunk_index=4,
                    content="Reference list",
                    char_start=0,
                    char_end=14,
                    chunk_metadata={"header_1": "References", "mode": "text"},
                ),
                LocalDocumentChunk(
                    id=uuid.uuid4(),
                    tenant_id=other_tenant_id,
                    document_id=other_document_id,
                    chunk_index=5,
                    content="Another tenant's reference list",
                    char_start=0,
                    char_end=32,
                    chunk_metadata={"header_1": "Bibliography", "mode": "text"},
                ),
            ]
        )
        db.commit()

        service = object.__new__(LocalRetrievalService)
        service.db = db
        results = service.get_document_references(
            tenant_id=tenant_id,
            document_ids=[document_id, other_document_id],
        )
        chunks = UnifiedChunksRepository(
            db,
            SimpleNamespace(
                local_vector_enabled=True,
                database_url="sqlite+pysqlite:///:memory:",
            ),
        )
        assert (
            chunks.count_by_document_id(
                tenant_id=tenant_id,
                document_id=document_id,
            )
            == 1
        )
        adapted_chunks = chunks.get_by_document_id(
            tenant_id=tenant_id,
            document_id=document_id,
        )

    assert len(results) == 1
    assert results[0].document_id == document_id
    assert results[0].filename == "sources.pdf"
    assert results[0].section_header == "References"
    assert len(adapted_chunks) == 1
    assert adapted_chunks[0].id == results[0].chunk_id
    assert adapted_chunks[0].content == "Reference list"
    engine.dispose()
