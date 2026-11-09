from __future__ import annotations

import uuid

import pytest

from app.local_vector.retrieval import LocalRetrievalService, RetrievedChunk

pytestmark = pytest.mark.unit_no_db


def test_quality_boost_preserves_chunk_position_and_immutable_input() -> None:
    service = object.__new__(LocalRetrievalService)
    original = RetrievedChunk(
        document_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        filename="guide.md",
        content="retrieved text",
        similarity_score=0.95,
        chunk_index=7,
        quality_score=1.0,
    )

    boosted = service._apply_quality_boost([original])

    assert original.similarity_score == 0.95
    assert boosted[0].similarity_score == 1.0
    assert boosted[0].chunk_index == 7
