"""Tests for session reference models and utilities (non-DB parts)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.deepspace.services.session_reference import (
    SessionReference,
    SessionReferenceService,
    SessionSnapshotRef,
    create_session_reference_service,
)


class TestSessionSnapshotRef:
    """Tests for the SessionSnapshotRef dataclass."""

    def test_creation(self) -> None:
        now = datetime.now(UTC)
        conversation_id = uuid.uuid4()

        snapshot = SessionSnapshotRef(
            conversation_id=conversation_id,
            turn=5,
            summary="Test summary",
            token_count=100,
            created_at=now,
            event_types=["plan/reviewed", "feedback/given"],
            key_decisions=["Approved plan A"],
            open_questions=["How to handle X?"],
        )

        assert snapshot.conversation_id == conversation_id
        assert snapshot.turn == 5
        assert snapshot.summary == "Test summary"
        assert snapshot.token_count == 100
        assert snapshot.created_at == now
        assert snapshot.event_types == ["plan/reviewed", "feedback/given"]
        assert snapshot.key_decisions == ["Approved plan A"]
        assert snapshot.open_questions == ["How to handle X?"]

    def test_default_values(self) -> None:
        now = datetime.now(UTC)
        conversation_id = uuid.uuid4()

        snapshot = SessionSnapshotRef(
            conversation_id=conversation_id,
            turn=1,
            summary="Test",
            token_count=10,
            created_at=now,
            event_types=["test"],
        )

        assert snapshot.key_decisions == []
        assert snapshot.open_questions == []


class TestSessionReference:
    """Tests for the SessionReference dataclass."""

    def test_creation(self) -> None:
        now = datetime.now(UTC)
        source_id = uuid.uuid4()
        target_id = uuid.uuid4()
        snapshot = SessionSnapshotRef(
            conversation_id=source_id,
            turn=3,
            summary="Snapshot summary",
            token_count=50,
            created_at=now,
            event_types=["test"],
        )

        ref = SessionReference(
            reference_id="ref-123",
            source_conversation_id=source_id,
            target_conversation_id=target_id,
            snapshot=snapshot,
            created_at=now,
            access_count=5,
            metadata={"source": "test"},
        )

        assert ref.reference_id == "ref-123"
        assert ref.source_conversation_id == source_id
        assert ref.target_conversation_id == target_id
        assert ref.snapshot == snapshot
        assert ref.created_at == now
        assert ref.access_count == 5
        assert ref.metadata == {"source": "test"}

    def test_default_values(self) -> None:
        now = datetime.now(UTC)
        source_id = uuid.uuid4()
        target_id = uuid.uuid4()
        snapshot = SessionSnapshotRef(
            conversation_id=source_id,
            turn=1,
            summary="Test",
            token_count=10,
            created_at=now,
            event_types=["test"],
        )

        ref = SessionReference(
            reference_id="ref-123",
            source_conversation_id=source_id,
            target_conversation_id=target_id,
            snapshot=snapshot,
            created_at=now,
        )

        assert ref.access_count == 0
        assert ref.metadata == {}


class TestSessionReferenceServiceNonDB:
    """Tests for SessionReferenceService methods that don't require database."""

    def test_build_context_injection(self) -> None:
        # Create a mock DB session
        mock_db = MagicMock()
        service = SessionReferenceService(mock_db)

        source_id = uuid.uuid4()
        target_id = uuid.uuid4()
        snapshot = SessionSnapshotRef(
            conversation_id=source_id,
            turn=10,
            summary="Conversation summary with decisions and questions",
            token_count=200,
            created_at=datetime.now(UTC),
            event_types=["plan/reviewed", "feedback/given"],
            key_decisions=["Approved plan X"],
            open_questions=["How to handle Y?"],
        )

        reference = SessionReference(
            reference_id="ref-test",
            source_conversation_id=source_id,
            target_conversation_id=target_id,
            snapshot=snapshot,
            created_at=datetime.now(UTC),
        )

        context = service.build_context_injection(reference)

        assert "Cross-session reference" in context
        assert str(source_id)[:8] in context
        assert "turn 10" in context
        assert "Conversation summary with decisions and questions" in context

    def test_build_context_injection_empty_snapshot(self) -> None:
        mock_db = MagicMock()
        service = SessionReferenceService(mock_db)

        source_id = uuid.uuid4()
        target_id = uuid.uuid4()
        snapshot = SessionSnapshotRef(
            conversation_id=source_id,
            turn=1,
            summary="",
            token_count=0,
            created_at=datetime.now(UTC),
            event_types=[],
        )

        reference = SessionReference(
            reference_id="ref-empty",
            source_conversation_id=source_id,
            target_conversation_id=target_id,
            snapshot=snapshot,
            created_at=datetime.now(UTC),
        )

        context = service.build_context_injection(reference)

        assert "Cross-session reference" in context
        assert str(source_id)[:8] in context
        assert "turn 1" in context


class TestCreateSessionReferenceService:
    """Tests for the factory function."""

    def test_create_session_reference_service(self) -> None:
        mock_db = MagicMock()
        service = create_session_reference_service(mock_db)

        assert isinstance(service, SessionReferenceService)
        assert service.db is mock_db
        assert service.event_store is not None


class TestDataclassImmutability:
    """Tests that dataclasses are frozen (immutable)."""

    def test_session_snapshot_ref_frozen(self) -> None:
        snapshot = SessionSnapshotRef(
            conversation_id=uuid.uuid4(),
            turn=1,
            summary="Test",
            token_count=10,
            created_at=datetime.now(UTC),
            event_types=["test"],
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.turn = 2

    def test_session_reference_frozen(self) -> None:
        snapshot = SessionSnapshotRef(
            conversation_id=uuid.uuid4(),
            turn=1,
            summary="Test",
            token_count=10,
            created_at=datetime.now(UTC),
            event_types=["test"],
        )

        reference = SessionReference(
            reference_id="ref-1",
            source_conversation_id=uuid.uuid4(),
            target_conversation_id=uuid.uuid4(),
            snapshot=snapshot,
            created_at=datetime.now(UTC),
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            reference.access_count = 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
