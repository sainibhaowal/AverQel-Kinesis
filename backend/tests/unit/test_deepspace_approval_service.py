from __future__ import annotations

import pytest

from app.deepspace.services.approvals import (
    ApprovalRequest,
    ApprovalService,
    init_approval_service,
)

pytestmark = pytest.mark.unit_no_db


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="write",
        arguments={"path": "notes.txt"},
        conversation_id="conversation-1",
        call_id="call-1",
        risk_level="high",
        permission_mode="workspace-write",
        warning="This writes a workspace file.",
        tenant_id="tenant-1",
        user_id="user-1",
    )


@pytest.mark.asyncio
async def test_missing_answerer_fails_closed() -> None:
    service = ApprovalService()

    outcome = await service.request(_request())

    assert outcome == "unavailable"
    assert service.audit_log() == []


@pytest.mark.asyncio
async def test_registered_answerer_decides_and_audits() -> None:
    service = ApprovalService()
    seen: list[ApprovalRequest] = []

    def answerer(request: ApprovalRequest) -> str:
        seen.append(request)
        return "allowed-once"

    dispose = service.register_answerer(answerer)
    outcome = await service.request(_request())

    assert outcome == "allowed-once"
    assert seen and seen[0].tool_name == "write"
    assert len(service.audit_log()) == 1
    assert service.audit_log()[0].outcome == "allowed-once"

    dispose()
    assert service.answerer_count() == 0
    assert await service.request(_request()) == "unavailable"


@pytest.mark.asyncio
async def test_async_answerer_is_awaited() -> None:
    service = ApprovalService()

    async def answerer(request: ApprovalRequest) -> str:
        assert request.call_id == "call-1"
        return "rejected"

    service.register_answerer(answerer)

    assert await service.request(_request()) == "rejected"


@pytest.mark.asyncio
async def test_unavailable_outcomes_delegate_to_the_next_answerer() -> None:
    service = ApprovalService()
    service.register_answerer(lambda request: "unavailable")
    service.register_answerer(lambda request: "cancelled")

    assert await service.request(_request()) == "cancelled"
    assert service.answerer_count() == 2


@pytest.mark.asyncio
async def test_failing_answerer_is_skipped() -> None:
    service = ApprovalService()

    def broken(request: ApprovalRequest) -> str:
        raise RuntimeError("answerer is offline")

    service.register_answerer(broken)
    service.register_answerer(lambda request: "allowed-once")

    assert await service.request(_request()) == "allowed-once"


@pytest.mark.asyncio
async def test_unknown_outcome_is_not_a_grant() -> None:
    service = ApprovalService()
    service.register_answerer(lambda request: "maybe")

    assert await service.request(_request()) == "unavailable"
    assert service.audit_log() == []


@pytest.mark.asyncio
async def test_duplicate_registration_is_disposed_once() -> None:
    service = ApprovalService()

    def answerer(request: ApprovalRequest) -> str:
        return "allowed-once"

    first = service.register_answerer(answerer)
    second = service.register_answerer(answerer)
    assert service.answerer_count() == 1

    second()
    first()
    assert service.answerer_count() == 0


def test_process_service_can_be_replaced() -> None:
    service = init_approval_service()

    assert service.answerer_count() == 0
