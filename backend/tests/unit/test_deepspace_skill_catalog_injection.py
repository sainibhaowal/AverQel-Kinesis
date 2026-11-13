"""Tests for per-turn skill catalog injection."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.deepspace.services import workspace_instructions
from app.deepspace.services.skill_registry import (
    SkillDefinition,
    SkillInvocationPolicy,
    SkillRegistry,
)
from app.deepspace.services.workspace_instructions import prepare_skill_catalog_message

pytestmark = pytest.mark.unit_no_db


class _Provider:
    def __init__(self, name: str, items) -> None:
        self.name = name
        self.items = items

    def discover(self):
        if isinstance(self.items, Exception):
            raise self.items
        return list(self.items)


def _skill(
    name: str,
    *,
    admin_only: bool = False,
    model_invocable: bool = True,
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=f"{name} description",
        when_to_use="",
        invocation=SkillInvocationPolicy(model_invocable=model_invocable, admin_only=admin_only),
        source="project",
        provider="filesystem",
        rank=100,
        content=f"{name} instructions",
    )


def _settings(**values):
    defaults = {
        "deepspace_skill_catalog_enabled": True,
        "deepspace_skill_catalog_max_chars": 12_000,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _auth(**values):
    defaults = {
        "tenant_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "roles": frozenset(),
        "permissions": frozenset(),
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _wire(monkeypatch, registry: SkillRegistry, root) -> None:
    monkeypatch.setattr(
        workspace_instructions, "get_workspace_roots", lambda _settings, _auth: (root, root)
    )
    monkeypatch.setattr(workspace_instructions, "skill_registry", lambda _settings, _root: registry)


def test_catalog_message_lists_visible_skills(monkeypatch, tmp_path) -> None:
    registry = SkillRegistry([_Provider("filesystem", [_skill("alpha"), _skill("beta")])])
    _wire(monkeypatch, registry, tmp_path)

    message = prepare_skill_catalog_message(_settings(), _auth())

    assert message is not None
    assert message["role"] == "system"
    assert message["source"]["kind"] == "skill-catalog"
    assert [entry["name"] for entry in message["source"]["entries"]] == ["alpha", "beta"]
    assert "alpha" in message["content"] and "beta" in message["content"]
    assert "<available_skills>" in message["content"]


def test_admin_only_skills_follow_the_principal(monkeypatch, tmp_path) -> None:
    registry = SkillRegistry(
        [
            _Provider(
                "filesystem",
                [_skill("alpha"), _skill("admin-tool", admin_only=True)],
            )
        ]
    )
    _wire(monkeypatch, registry, tmp_path)

    plain = prepare_skill_catalog_message(_settings(), _auth())
    assert plain is not None
    assert [entry["name"] for entry in plain["source"]["entries"]] == ["alpha"]

    admin = prepare_skill_catalog_message(_settings(), _auth(roles=frozenset({"admin"})))
    assert admin is not None
    assert [entry["name"] for entry in admin["source"]["entries"]] == [
        "admin-tool",
        "alpha",
    ]

    permitted = prepare_skill_catalog_message(
        _settings(), _auth(permissions=frozenset({"skills:admin"}))
    )
    assert permitted is not None
    assert "admin-tool" in [entry["name"] for entry in permitted["source"]["entries"]]


def test_catalog_can_be_disabled_and_empty_catalogs_are_skipped(monkeypatch, tmp_path) -> None:
    registry = SkillRegistry([_Provider("filesystem", [])])
    _wire(monkeypatch, registry, tmp_path)

    assert prepare_skill_catalog_message(_settings(), _auth()) is None
    assert (
        prepare_skill_catalog_message(_settings(deepspace_skill_catalog_enabled=False), _auth())
        is None
    )


def test_empty_incomplete_catalog_is_still_published(monkeypatch, tmp_path) -> None:
    registry = SkillRegistry([_Provider("remote", RuntimeError("offline"))])
    _wire(monkeypatch, registry, tmp_path)

    message = prepare_skill_catalog_message(_settings(), _auth())

    assert message is not None
    assert message["source"]["incomplete"] is True
    assert "No skills could be confirmed" in message["content"]


def test_catalog_budget_marks_a_shortened_list(monkeypatch, tmp_path) -> None:
    registry = SkillRegistry([_Provider("filesystem", [_skill("alpha"), _skill("beta")])])
    _wire(monkeypatch, registry, tmp_path)

    message = prepare_skill_catalog_message(_settings(), _auth(), max_chars=1)

    # A budget too small for even one entry publishes no catalog; the live
    # registry remains available to skill_list.
    assert message is None

    message = prepare_skill_catalog_message(_settings(), _auth(), max_chars=50)
    assert message is not None
    assert message["source"]["truncated"] is True


def test_workspace_unavailable_is_not_an_error(monkeypatch, tmp_path) -> None:
    from app.deepspace.services.workspace_root import WorkspaceUnavailableError

    def unavailable(_settings, _auth):
        raise WorkspaceUnavailableError("no workspace")

    monkeypatch.setattr(workspace_instructions, "get_workspace_roots", unavailable)
    assert prepare_skill_catalog_message(_settings(), _auth()) is None
