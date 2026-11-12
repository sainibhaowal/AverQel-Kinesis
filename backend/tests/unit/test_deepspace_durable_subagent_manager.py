from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.deepspace.models.conversation import Conversation
from app.deepspace.models.subagent import DeepSpaceSubagentRun
from app.deepspace.services.durable_subagent_manager import (
    DurableSubagentManager,
    get_durable_subagent_manager,
)

pytestmark = pytest.mark.unit_no_db


def _db() -> tuple[Session, uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Conversation.__table__.create(engine)
    DeepSpaceSubagentRun.__table__.create(engine)
    session = Session(engine)
    tenant_id, user_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session.add(
        Conversation(
            id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            title="subagent manager test",
        )
    )
    session.commit()
    return session, tenant_id, user_id, conversation_id


def _child(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    parent_id: uuid.UUID | None = None,
    messages: list[object] | None = None,
) -> DeepSpaceSubagentRun:
    return DeepSpaceSubagentRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        parent_agent_id=parent_id,
        mode="continuable",
        label="child",
        delegation_depth=1,
        tool_filter={},
        descriptor={},
        inherited_history=[],
        pending_messages=list(messages or []),
        turn_history=[],
        status="queued",
    )


def test_admission_checks_conversation_ownership_and_child_budget() -> None:
    db, tenant_id, user_id, conversation_id = _db()
    manager = DurableSubagentManager()
    first = _child(tenant_id, user_id, conversation_id)
    assert manager.admit(db, first, max_children=1) is True

    second = _child(tenant_id, user_id, conversation_id)
    assert manager.admit(db, second, max_children=1) is False
    assert db.get(DeepSpaceSubagentRun, second.id) is None

    foreign = _child(uuid.uuid4(), user_id, conversation_id)
    assert manager.admit(db, foreign, max_children=10) is False
    assert db.get(DeepSpaceSubagentRun, foreign.id) is None
    db.close()


def test_expired_lease_can_be_reclaimed_and_old_worker_is_fenced() -> None:
    db, tenant_id, user_id, conversation_id = _db()
    manager = DurableSubagentManager(lease_seconds=30)
    child = _child(
        tenant_id,
        user_id,
        conversation_id,
        messages=[{"id": "m1", "message": "work", "attachment_file_ids": []}],
    )
    assert manager.admit(db, child, max_children=5)

    first = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=child.id,
    )
    assert first.status == "claimed"
    assert first.lease is not None
    assert first.messages[0]["message"] == "work"
    assert manager.heartbeat(db, first.lease) is True

    stored = db.get(DeepSpaceSubagentRun, child.id)
    assert stored is not None
    stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    second = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=child.id,
        message="second activation",
    )
    assert second.status == "claimed"
    assert second.lease is not None
    assert second.lease.token != first.lease.token
    assert manager.owns_lease(db, second.lease) is True
    assert (
        manager.release(
            db,
            first.lease,
            status="completed",
            stop_reason="completed",
        )
        is False
    )
    assert (
        manager.release(
            db,
            second.lease,
            status="idle",
            stop_reason="completed",
            result_text="done",
        )
        is True
    )
    assert manager.owns_lease(db, second.lease) is False
    db.close()


def test_cancel_marks_the_entire_owned_descendant_tree() -> None:
    db, tenant_id, user_id, conversation_id = _db()
    manager = DurableSubagentManager()
    root = _child(tenant_id, user_id, conversation_id)
    descendant = _child(tenant_id, user_id, conversation_id, parent_id=root.id)
    grandchild = _child(tenant_id, user_id, conversation_id, parent_id=descendant.id)
    for active in (root, descendant, grandchild):
        active.busy = True
        active.status = "running"
    assert manager.admit(db, root, max_children=10)
    assert manager.admit(db, descendant, max_children=10)
    assert manager.admit(db, grandchild, max_children=10)

    count = manager.request_cancel(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=root.id,
    )
    assert count == 3
    assert all(
        db.get(DeepSpaceSubagentRun, child.id).cancel_requested
        for child in (root, descendant, grandchild)
    )
    db.close()


def test_cancel_flag_is_honored_before_claiming_pending_work() -> None:
    db, tenant_id, user_id, conversation_id = _db()
    manager = DurableSubagentManager()
    child = _child(
        tenant_id,
        user_id,
        conversation_id,
        messages=[{"message": "keep this queued"}],
    )
    child.cancel_requested = True
    assert manager.admit(db, child, max_children=5)

    result = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=child.id,
    )
    assert result.status == "cancelled"
    stored = db.get(DeepSpaceSubagentRun, child.id)
    assert stored is not None
    assert stored.pending_messages == [{"message": "keep this queued"}]
    assert stored.cancel_requested is False
    db.close()


def test_lease_duration_must_allow_a_safe_heartbeat_window() -> None:
    with pytest.raises(ValueError, match="at least 30 seconds"):
        DurableSubagentManager(lease_seconds=29)


def test_claim_reports_missing_busy_legacy_and_idle_children() -> None:
    db, tenant_id, user_id, conversation_id = _db()
    manager = DurableSubagentManager()

    missing = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=uuid.uuid4(),
    )
    assert missing.status == "missing"

    legacy = _child(tenant_id, user_id, conversation_id)
    legacy.busy = True
    legacy.lease_token = None
    assert manager.admit(db, legacy, max_children=5)
    busy = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=legacy.id,
    )
    assert busy.status == "busy"

    active = _child(tenant_id, user_id, conversation_id)
    active.busy = True
    active.lease_token = uuid.uuid4().hex
    active.lease_expires_at = datetime.now() + timedelta(minutes=1)
    assert manager.admit(db, active, max_children=5)
    active_claim = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=active.id,
    )
    assert active_claim.status == "busy"

    idle = _child(tenant_id, user_id, conversation_id)
    assert manager.admit(db, idle, max_children=5)
    result = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=idle.id,
    )
    assert result.status == "idle"
    db.close()


def test_release_can_settle_result_and_increment_turn_count() -> None:
    db, tenant_id, user_id, conversation_id = _db()
    manager = DurableSubagentManager()
    child = _child(
        tenant_id,
        user_id,
        conversation_id,
        messages=[{"message": "work"}],
    )
    assert manager.admit(db, child, max_children=5)
    claim = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=child.id,
    )
    assert claim.lease is not None
    assert manager.release(
        db,
        claim.lease,
        status="completed",
        stop_reason="completed",
        result_text="finished",
        increment_turn=True,
    )
    stored = db.get(DeepSpaceSubagentRun, child.id)
    assert stored is not None
    assert stored.result_text == "finished"
    assert stored.turn_count == 1

    second_child = _child(
        tenant_id,
        user_id,
        conversation_id,
        messages=[{"message": "another turn"}],
    )
    assert manager.admit(db, second_child, max_children=5)
    second_claim = manager.claim_turn(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        child_id=second_child.id,
    )
    assert second_claim.lease is not None
    assert manager.release(
        db,
        second_claim.lease,
        status="idle",
        stop_reason="yielded",
    )
    db.close()


def test_cancel_can_target_only_the_root_and_ignores_idle_descendants() -> None:
    db, tenant_id, user_id, conversation_id = _db()
    manager = DurableSubagentManager()
    root = _child(tenant_id, user_id, conversation_id)
    descendant = _child(tenant_id, user_id, conversation_id, parent_id=root.id)
    idle_descendant = _child(tenant_id, user_id, conversation_id, parent_id=descendant.id)
    for child in (root, descendant, idle_descendant):
        assert manager.admit(db, child, max_children=5)

    assert (
        manager.request_cancel(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            child_id=uuid.uuid4(),
        )
        == 0
    )
    assert (
        manager.request_cancel(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            child_id=root.id,
            include_descendants=False,
        )
        == 1
    )
    assert db.get(DeepSpaceSubagentRun, root.id).cancel_requested is True
    assert db.get(DeepSpaceSubagentRun, descendant.id).cancel_requested is False

    # Descendants are traversed to completion, but only active descendants are
    # included in the cancellation request.
    db.get(DeepSpaceSubagentRun, root.id).cancel_requested = False
    active = db.get(DeepSpaceSubagentRun, descendant.id)
    assert active is not None
    active.busy = True
    active.status = "running"
    db.commit()
    assert (
        manager.request_cancel(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            child_id=root.id,
        )
        == 2
    )
    assert db.get(DeepSpaceSubagentRun, idle_descendant.id).cancel_requested is False
    db.close()


def test_manager_accessor_returns_the_shared_stateless_manager() -> None:
    assert get_durable_subagent_manager() is get_durable_subagent_manager()
