"""Authorization tests for the DeepSpace slash-command endpoint."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.deepspace.api.host import routes

pytestmark = pytest.mark.unit_no_db


def test_slash_command_requires_the_authenticated_users_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    observed: dict[str, Any] = {}

    class _Repository:
        def __init__(self, _db: object) -> None:
            pass

        def get_conversation(self, **kwargs: Any) -> None:
            observed.update(kwargs)
            return None

    monkeypatch.setattr(
        "app.deepspace.repositories.chat.DeepSpaceChatRepository",
        _Repository,
    )
    monkeypatch.setattr(
        routes,
        "get_slash_command_registry",
        lambda *_args, **_kwargs: pytest.fail("unowned conversation reached the registry"),
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            routes.execute_slash_command(
                str(conversation_id),
                routes.SlashCommandExecuteRequest(input="/help"),
                SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
                object(),  # type: ignore[arg-type]
            )
        )

    assert error.value.status_code == 404
    assert observed == {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
    }
