"""Tests for the deployment-settings chat provider fallback."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.platform.database.session import Session
from app.providers.services.selection_service import ProviderSelectionService

pytestmark = pytest.mark.unit_no_db


class _NoAssignments:
    """A deployment without database provider assignments."""

    def get_active_assignment(self, **_: Any) -> None:
        return None


class _NoProviders:
    """A deployment without database provider configurations."""

    def list_by_workspace(self, **_: Any) -> list[Any]:
        return []


class _Audit:
    """A no-op audit sink for the pure selection test."""

    def write_event(self, **_: Any) -> None:
        return None


def test_settings_chat_provider_is_available_without_a_database_provider_row(
    settings: Settings,
) -> None:
    """Direct/headless runs use deployment LLM settings as a final fallback."""
    configured = settings.model_copy(
        update={
            "llm_provider": "larprouter",
            "llm_model": "deepseek-v4.1-flash",
            "llm_api_base_url": "https://larprouter.example/v1",
            "llm_api_key": "test-settings-key",
        }
    )
    service = ProviderSelectionService(cast(Session, object()), configured)
    service.assignments = cast(Any, _NoAssignments())
    service.configs = cast(Any, _NoProviders())
    service.audit = cast(Any, _Audit())

    selection = service.resolve_chat(
        tenant_id=uuid.uuid4(),
        actor_user_id=None,
        record_audit=False,
    )

    assert len(selection.candidates) == 1
    candidate = selection.candidates[0]
    assert candidate.provider_type == "larprouter"
    assert candidate.model_name == "deepseek-v4.1-flash"
    assert candidate.provider_config_id is None
    assert candidate.source == "env_fallback"
    assert candidate.base_url == "https://larprouter.example/v1"
    assert candidate.api_key == "test-settings-key"
    assert "settings-chat:larprouter:deepseek-v4.1-flash" in selection.selection_notes
