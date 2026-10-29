"""Tests for DeepSpace session-format migration behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.deepspace.services.session_format_migration import (
    SessionFormatMigrationChain,
    SessionFormatMigrationConfig,
)

pytestmark = pytest.mark.unit_no_db


def test_v1_tool_event_migrates_to_the_current_format() -> None:
    """The read migration adds current lineage and structured tool fields."""
    chain = SessionFormatMigrationChain(
        SessionFormatMigrationConfig(current_version=4),
        SimpleNamespace(),
    )

    migrated = chain.migrate_event(
        {
            "event_type": "tool/result",
            "payload": {"input": {"path": "README.md"}, "output": {"ok": True}},
        }
    )

    assert migrated["format_version"] == 4
    assert migrated["subagent_id"] is None
    assert migrated["run_id"] is None
    assert migrated["step"] is None
    assert migrated["payload"] == {
        "arguments": {"path": "README.md"},
        "result": {"ok": True},
    }


def test_disabled_migration_leaves_the_record_unchanged() -> None:
    """A deployment can disable automatic read migration explicitly."""
    event = {"event_type": "turn/start", "payload": {"value": 1}}
    chain = SessionFormatMigrationChain(
        SessionFormatMigrationConfig(enabled=False),
        SimpleNamespace(),
    )

    assert chain.migrate_event(event) is event
