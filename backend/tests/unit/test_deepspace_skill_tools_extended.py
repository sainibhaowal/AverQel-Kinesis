"""Tests for the extended skill tools."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services.skill_registry import (
    SkillDefinition,
    SkillInvocationPolicy,
    SkillRegistry,
)
from app.deepspace.services.skill_telemetry import reset_skill_counters, skill_counters
from app.deepspace.services.tools import skill as skill_tools

pytestmark = pytest.mark.unit_no_db


class _Provider:
    def __init__(self, name: str, items) -> None:
        self.name = name
        self.items = items

    def discover(self):
        return list(self.items)


def _skill(
    name: str,
    *,
    content: str = "instructions",
    provider: str = "filesystem",
    source: str = "project",
    admin_only: bool = False,
    model_invocable: bool = True,
    user_invocable: bool = True,
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=f"{name} description",
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


def _settings(**values):
    defaults = {
        "deepspace_skill_custom_roots": "",
        "deepspace_bundled_skills_enabled": False,
        "deepspace_skill_user_invocation_enabled": True,
        "deepspace_skill_telemetry_enabled": False,
        "deepspace_permission_mode": "workspace-write",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _context(root: Path, *, roles=frozenset(), permissions=frozenset()) -> SimpleNamespace:
    return SimpleNamespace(
        workspace_root=root,
        settings=_settings(),
        auth=SimpleNamespace(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            roles=frozenset(roles),
            permissions=frozenset(permissions),
        ),
        conversation_id=uuid.uuid4(),
    )


def _registry(monkeypatch, *skills: SkillDefinition) -> None:
    registry = SkillRegistry([_Provider("filesystem", list(skills))])
    monkeypatch.setattr(skill_tools, "skill_registry", lambda *_args, **_kwargs: registry)


@pytest.fixture(autouse=True)
def _clean_counters():
    reset_skill_counters()
    yield
    reset_skill_counters()


async def test_list_reports_provenance_and_hides_admin_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(
        monkeypatch,
        _skill("visible", content="body"),
        _skill("admin-tool", admin_only=True),
        _skill("model-hidden", model_invocable=False),
    )
    context = _context(tmp_path)

    result = await skill_tools._skill_list({}, context)

    assert result["status"] == "ok"
    assert [item["name"] for item in result["skills"]] == ["visible"]
    summary = result["skills"][0]
    assert summary["provider"] == "filesystem"
    assert summary["rank"] == 100
    assert summary["content_sha256"]
    assert summary["invocation"]["admin_only"] is False
    assert skill_counters()["list:ok"] == 1


async def test_list_includes_admin_skills_for_an_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch, _skill("admin-tool", admin_only=True))
    context = _context(tmp_path, roles={"admin"})

    result = await skill_tools._skill_list({}, context)

    assert [item["name"] for item in result["skills"]] == ["admin-tool"]


async def test_list_scope_and_query_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch, _skill("alpha"), _skill("beta"))
    context = _context(tmp_path)

    filtered = skill_tools.workspace_skills(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        workspace_root=tmp_path,
        query="alp",
        include_shared=False,
    )
    assert [item["name"] for item in filtered] == ["alpha"]

    from app.deepspace.services.skill_scope import child_scope, root_scope

    scoped = skill_tools.workspace_skills(
        context.settings,
        tenant_id=context.auth.tenant_id,
        user_id=context.auth.user_id,
        workspace_root=tmp_path,
        include_shared=False,
        scope=child_scope(root_scope(), "child", granted=["beta"]),
    )
    assert [item["name"] for item in scoped] == ["beta"]


async def test_load_returns_new_provenance_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch, _skill("demo", content="Do the work"))
    context = _context(tmp_path)

    result = await skill_tools._skill_load({"name": "demo"}, context)

    assert result["status"] == "ok"
    assert result["provider"] == "filesystem"
    assert result["rank"] == 100
    assert result["content_sha256"]
    assert result["invocation"] == {
        "model_invocable": True,
        "user_invocable": True,
        "admin_only": False,
    }
    assert result["instructions"] == "Do the work"
    assert skill_counters()["load:ok"] == 1


async def test_load_and_resource_enforce_admin_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch, _skill("admin-tool", admin_only=True))

    plain = _context(tmp_path)
    assert (await skill_tools._skill_load({"name": "admin-tool"}, plain))["status"] == "not_found"
    assert (await skill_tools._skill_resource({"name": "admin-tool", "path": "ref.md"}, plain))[
        "status"
    ] == "not_found"

    admin = _context(tmp_path, roles={"admin"})
    assert (await skill_tools._skill_load({"name": "admin-tool"}, admin))["status"] == "ok"


async def test_resource_reads_utf8_and_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "demo"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Instructions", encoding="utf-8")
    (skill_dir / "references" / "guide.md").write_text("# Guide", encoding="utf-8")
    (skill_dir / "references" / "blob.bin").write_bytes(b"\xff\xfe\x00")
    context = _context(tmp_path)

    text = await skill_tools._skill_resource(
        {"name": "demo", "path": "references/guide.md"}, context
    )
    assert text["status"] == "ok"
    assert text["encoding"] == "utf-8"
    assert text["content"] == "# Guide"

    binary = await skill_tools._skill_resource(
        {"name": "demo", "path": "references/blob.bin"}, context
    )
    assert binary["status"] == "ok"
    assert binary["encoding"] == "base64"

    missing = await skill_tools._skill_resource(
        {"name": "demo", "path": "references/missing.md"}, context
    )
    assert missing["status"] == "error"
    assert skill_counters()["resource:ok"] == 2
    assert skill_counters()["resource:error"] == 1


async def test_validate_accepts_and_rejects_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry(monkeypatch, _skill("existing"))
    context = _context(tmp_path)

    valid = await skill_tools._skill_validate(
        {
            "content": "---\nname: fresh\ndescription: Fresh skill\n---\nLong enough instructions.",
        },
        context,
    )
    assert valid["status"] == "ok" and valid["valid"] is True
    assert valid["issues"] == []

    duplicate = await skill_tools._skill_validate(
        {
            "content": "---\nname: existing\ndescription: Duplicate\n---\nLong enough instructions.",
        },
        context,
    )
    assert duplicate["valid"] is True
    assert any(issue["code"] == "name_duplicate" for issue in duplicate["issues"])

    invalid = await skill_tools._skill_validate(
        {"content": "---\nname: Bad Name\n---\nbody"}, context
    )
    assert invalid["status"] == "invalid"
    assert invalid["valid"] is False
    assert skill_counters()["validate:invalid"] == 1
