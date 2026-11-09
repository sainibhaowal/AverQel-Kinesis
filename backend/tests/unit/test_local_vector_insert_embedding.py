"""Tests for binding embeddings into SQLite's local vector table."""

import struct

import pytest
from sqlalchemy import create_engine, text

from app.local_vector.models import insert_embedding

pytestmark = pytest.mark.unit_no_db


def test_insert_embedding_binds_vector_blob_to_named_parameters() -> None:
    """Embedding writes use SQLAlchemy's named-bind execution contract."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE vec_chunks (chunk_id TEXT PRIMARY KEY, embedding BLOB)")
            )

        insert_embedding(engine, "chunk-1", [0.25, -0.5, 1.0])

        with engine.connect() as connection:
            stored = connection.execute(
                text("SELECT embedding FROM vec_chunks WHERE chunk_id = :chunk_id"),
                {"chunk_id": "chunk-1"},
            ).scalar_one()

        assert struct.unpack("3f", stored) == pytest.approx((0.25, -0.5, 1.0))
    finally:
        engine.dispose()
