"""Tests for the model-facing skill catalog."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.deepspace.services.skill_catalog import (
    CATALOG_CLOSE,
    CATALOG_OPEN,
    SkillCatalogEntry,
    SkillCatalogSnapshot,
    build_skill_catalog,
    escape_skill_text,
    normalize_skill_line,
    render_skill_catalog,
    skill_catalog_message,
)
from app.deepspace.services.skill_registry import (
    SkillDefinition,
    SkillInvocationPolicy,
    SkillProviderObservation,
    SkillRegistry,
)
from app.deepspace.services.skill_scope import child_scope, root_scope

pytestmark = pytest.mark.unit_no_db


def _skill(
    name: str,
    *,
    description: str = "",
    provider: str = "filesystem",
    source: str = "project",
    model_invocable: bool = True,
    user_invocable: bool = True,
    admin_only: bool = False,
    content: str = "Instructions",
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=description or f"{name} description",
        when_to_use="",
        invocation=SkillInvocationPolicy(
            model_invocable=model_invocable,
            user_invocable=user_invocable,
            admin_only=admin_only,
        ),
        source=source,
        provider=provider,
        rank=100,
        content=content,
    )


class _Provider:
    def __init__(self, name: str, items: list[SkillDefinition] | Exception) -> None:
        self.name = name
        self.items = items
        self.calls = 0

    def discover(self) -> list[SkillDefinition]:
        self.calls += 1
        if isinstance(self.items, Exception):
            raise self.items
        return self.items


def _registry(*providers: Any) -> SkillRegistry:
    return SkillRegistry(list(providers))


def test_escape_text_removes_controls_and_escapes_markup() -> None:
    assert escape_skill_text("a\x00b\x1fc") == "abc"
    assert escape_skill_text('<x a="b">&</x>') == "&lt;x a=&quot;b&quot;&gt;&amp;&lt;/x&gt;"
    assert escape_skill_text(None) == ""  # type: ignore[arg-type]


def test_normalize_line_collapses_whitespace_and_bounds_length() -> None:
    assert normalize_skill_line("  one\n\t two  ") == "one two"
    shortened = normalize_skill_line("a" * 20, limit=10)
    assert shortened == "a" * 9 + "\u2026"
    assert len(shortened) == 10
    assert normalize_skill_line("a" * 20, limit=0) == "a" * 20
    assert normalize_skill_line("<b>", limit=1) == "\u2026"


def test_build_catalog_sorts_filters_and_records_provenance() -> None:
    registry = _registry(
        _Provider(
            "filesystem",
            [
                _skill("zeta", description="Last"),
                _skill("Alpha", description="First <one>"),
            ],
        )
    )
    snapshot = build_skill_catalog(registry)

    assert [entry.name for entry in snapshot.entries] == ["Alpha", "zeta"]
    assert snapshot.incomplete is False
    assert snapshot.truncated is False
    assert len(snapshot.digest) == 64
    assert snapshot.char_count > 0
    assert snapshot.entries[0].content_sha256
    assert snapshot.to_dict()["entries"][0]["name"] == "Alpha"


def test_build_catalog_honors_model_and_admin_visibility() -> None:
    registry = _registry(
        _Provider(
            "filesystem",
            [
                _skill("visible"),
                _skill("user-only", model_invocable=False),
                _skill("admin-only", admin_only=True),
            ],
        )
    )

    model_snapshot = build_skill_catalog(registry)
    assert [entry.name for entry in model_snapshot.entries] == ["visible"]

    admin_snapshot = build_skill_catalog(registry, for_admin=True)
    assert [entry.name for entry in admin_snapshot.entries] == ["admin-only", "visible"]

    all_snapshot = build_skill_catalog(registry, for_model=False)
    assert [entry.name for entry in all_snapshot.entries] == ["user-only", "visible"]


def test_build_catalog_reports_incomplete_provider_and_retries() -> None:
    broken = _Provider("remote", RuntimeError("offline"))
    registry = _registry(broken, _Provider("filesystem", [_skill("local")]))
    snapshot = build_skill_catalog(registry)

    assert snapshot.incomplete is True
    assert [entry.name for entry in snapshot.entries] == ["local"]
    assert broken.calls == 2


def test_build_catalog_truncates_by_budget_and_keeps_full_digest() -> None:
    registry = _registry(
        _Provider(
            "filesystem",
            [
                _skill("first", description="first description", provider="p"),
                _skill("second", description="second description", provider="p"),
            ],
        )
    )
    full = build_skill_catalog(registry)
    first_cost = (
        len(full.entries[0].name)
        + len(full.entries[0].description)
        + len(full.entries[0].provider)
        + 8
    )
    snapshot = build_skill_catalog(registry, max_chars=first_cost + 1)

    assert snapshot.truncated is True
    assert len(snapshot.entries) == 1
    assert snapshot.digest == full.digest
    assert snapshot.char_count <= first_cost + 1


def test_build_catalog_bounds_descriptions() -> None:
    registry = _registry(_Provider("filesystem", [_skill("short", description="x" * 900)]))
    snapshot = build_skill_catalog(registry, description_chars=12)

    assert len(snapshot.entries[0].description) == 12
    assert snapshot.entries[0].description == "x" * 12


def test_build_catalog_filters_to_the_agents_skill_scope() -> None:
    registry = _registry(
        _Provider(
            "filesystem",
            [_skill("visible"), _skill("hidden")],
        )
    )
    scope = child_scope(root_scope(), "child-1", granted=["VISIBLE"])

    snapshot = build_skill_catalog(registry, scope=scope)

    assert [entry.name for entry in snapshot.entries] == ["visible"]


def test_digest_is_independent_of_provider_order() -> None:
    one = build_skill_catalog(_registry(_Provider("a", [_skill("alpha"), _skill("beta")])))
    two = build_skill_catalog(_registry(_Provider("a", [_skill("beta"), _skill("alpha")])))
    assert one.digest == two.digest


def test_digest_changes_when_content_changes() -> None:
    one = build_skill_catalog(_registry(_Provider("a", [_skill("alpha", content="one")])))
    two = build_skill_catalog(_registry(_Provider("a", [_skill("alpha", content="two")])))
    assert one.digest != two.digest


def test_render_catalog_frames_entries_and_escapes_descriptions() -> None:
    snapshot = SkillCatalogSnapshot(
        entries=(
            SkillCatalogEntry(
                name="demo",
                description='Use <tag> & "quotes"',
                provider="filesystem",
            ),
        ),
        digest="digest",
        char_count=10,
    )
    rendered = render_skill_catalog(snapshot)

    assert rendered.startswith("<system-reminder>")
    assert rendered.endswith("</system-reminder>")
    assert CATALOG_OPEN in rendered and CATALOG_CLOSE in rendered
    assert "&lt;tag&gt; &amp; &quot;quotes&quot;" in rendered
    assert "skill_load" in rendered
    assert "@skill:name" in rendered


def test_render_catalog_states_truncation_and_incompleteness() -> None:
    truncated = render_skill_catalog(
        SkillCatalogSnapshot(
            entries=(SkillCatalogEntry(name="demo", description="d"),),
            truncated=True,
        )
    )
    assert "shortened" in truncated and "skill_list" in truncated

    incomplete = render_skill_catalog(SkillCatalogSnapshot(entries=(), incomplete=True))
    assert "No skills could be confirmed" in incomplete
    assert "incomplete" in incomplete.lower()

    empty = render_skill_catalog(SkillCatalogSnapshot(entries=()))
    assert "No skills are installed" in empty
    assert "incomplete" not in empty.lower()


def test_catalog_message_carries_source_metadata() -> None:
    snapshot = SkillCatalogSnapshot(
        entries=(SkillCatalogEntry(name="demo", description="d", provider="filesystem"),),
        incomplete=False,
        truncated=True,
        digest="abc",
        char_count=12,
    )
    message = skill_catalog_message(snapshot)

    assert message["role"] == "system"
    assert isinstance(message["content"], str)
    assert message["source"]["kind"] == "skill-catalog"
    assert message["source"]["digest"] == "abc"
    assert message["source"]["truncated"] is True
    assert message["source"]["entries"][0]["name"] == "demo"
    json.dumps(message["source"])


def test_snapshot_observation_shape_is_stable() -> None:
    observation = SkillProviderObservation(
        provider="filesystem", ok=False, discovered=0, attempts=2, error="boom"
    )
    assert observation.provider == "filesystem"
    assert observation.attempts == 2
    assert observation.error == "boom"
