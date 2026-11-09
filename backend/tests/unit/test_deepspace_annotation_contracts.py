from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Literal, cast, get_type_hints
from uuid import UUID, uuid4

import pytest

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.services.goal_driver import RoundAttempt
from app.deepspace.services.runtime_invariants import CompositeInvariant, InvariantResult
from app.deepspace.services.session_export import ExportOptions, export_conversation_log
from app.deepspace.services.session_query import SessionQueryService
from app.deepspace.services.session_references import SessionReferenceResolver

pytestmark = pytest.mark.unit_no_db


def test_deepspace_literal_annotations_resolve_to_supported_values() -> None:
    assert get_type_hints(RoundAttempt)["phase"] == Literal["queued", "claimed", "admitted"]
    assert get_type_hints(InvariantResult)["severity"] == Literal["error", "warning", "info"]
    assert get_type_hints(CompositeInvariant.__init__)["operator"] == Literal["and", "or"]
    assert get_type_hints(ExportOptions)["format"] == Literal["jsonl", "json"]
    assert get_type_hints(export_conversation_log)["format"] == Literal["jsonl", "json"]


def test_session_reference_turn_limit_is_applied() -> None:
    class SessionQuery:
        def __init__(self) -> None:
            self.limits: list[int] = []

        async def get_trace(
            self,
            *,
            conversation_id: UUID,
            auth: AuthContext,
            limit: int,
        ) -> dict[str, Any]:
            del conversation_id, auth
            self.limits.append(limit)
            return {"turns": []}

    query = SessionQuery()
    resolver = SessionReferenceResolver(
        cast(SessionQueryService, query),
        cast(
            Settings,
            SimpleNamespace(
                deepspace_session_references_max_bytes=50_000,
                deepspace_session_references_max_refs=5,
            ),
        ),
    )

    asyncio.run(
        resolver.resolve(
            [f"session:{uuid4()}:20"],
            AuthContext(
                tenant_id=uuid4(),
                user_id=uuid4(),
                roles=frozenset(),
                token_id="unit-test",
            ),
            max_turns_per_session=2,
        )
    )

    assert query.limits == [2]
