"""Extended tests for the ranked skill registry."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.deepspace.services import skill_registry as registry_module
from app.deepspace.services.skill_registry import (
    FilesystemSkillProvider,
    RuntimeSkillProvider,
    SkillDefinition,
    SkillInvocationError,
    SkillInvocationPolicy,
    SkillRegistry,
    SkillRoot,
    expand_user_skill_invocation,
    is_valid_skill_name,
    parse_skill_document,
)
from app.deepspace.services.skill_scope import child_scope, root_scope

pytestmark = pytest.mark.unit_no_db


class _Provider:
    def __init__(self, name: str, items: list[SkillDefinition] | None = None) -> None:
        self.name = name
        self.items = items or []
        self.calls = 0

    def discover(self) -> list[SkillDefinition]:
        self.calls += 1
        return list(self.items)


def _skill(
    name: str,
    *,
    content: str | None = None,
    provider: str = "filesystem",
    source: str = "project",
    rank: int = 100,
    admin_only: bool = False,
    user_invocable: bool = True,
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=f"{name} description",
        when_to_use="",
        invocation=SkillInvocationPolicy(user_invocable=user_invocable, admin_only=admin_only),
        source=source,
        provider=provider,
        rank=rank,
        content=content if content is not None else f"{name} instructions",
    )


def _settings(**values):
    defaults = {
        "deepspace_skill_custom_roots": "",
        "deepspace_bundled_skills_enabled": False,
        "deepspace_skill_enabled_names": "",
        "deepspace_skill_disabled_names": "",
        "deepspace_skill_strict_names": False,
        "deepspace_skill_remote_url": "",
        "deepspace_skill_remote_public_key": "",
        "deepspace_skill_cache_ttl_seconds": 0.0,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_runtime_skill_provider_publishes_and_removes_at_rank_250(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "demo.md").write_text("Project demo instructions", encoding="utf-8")
    registry = SkillRegistry([FilesystemSkillProvider([(root, "project", 200)])])

    remove = registry.register_runtime_skill(_skill("plugin", content="Plugin body"))
    try:
        listing = {skill.name: skill for skill in registry.list()}
        assert listing["plugin"].provider == "runtime"
        assert listing["plugin"].rank == 250
        assert listing["plugin"].content == "Plugin body"
        assert listing["plugin"].content_sha256

        # A project root outranks a runtime skill with the same name.
        registry.register_runtime_skill(_skill("demo", content="Runtime demo"))
        assert registry.get("demo").content == "Project demo instructions"
    finally:
        remove()
        remove()
    assert [skill.name for skill in registry.list()] == ["demo"]


def test_runtime_provider_reuses_one_instance_and_lists_snapshot() -> None:
    registry = SkillRegistry([])
    first = registry.register_runtime_skill(_skill("one"))
    registry.register_runtime_skill(_skill("two"))
    runtimes = [
        provider for provider in registry._providers if isinstance(provider, RuntimeSkillProvider)
    ]
    assert len(runtimes) == 1
    assert {skill.name for skill in runtimes[0].discover()} == {"one", "two"}
    first()
    assert {skill.name for skill in runtimes[0].discover()} == {"two"}


class _FlakyProvider:
    def __init__(self, name: str, failures: int) -> None:
        self.name = name
        self.failures = failures
        self.calls = 0

    def discover(self) -> list[SkillDefinition]:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary failure")
        return [_skill("recovered", provider=self.name)]


class _AlwaysBrokenProvider:
    name = "broken"

    def __init__(self) -> None:
        self.calls = 0

    def discover(self) -> list[SkillDefinition]:
        self.calls += 1
        raise RuntimeError("permanently offline")


def test_registry_retries_once_and_reports_observations() -> None:
    flaky = _FlakyProvider("flaky", failures=1)
    broken = _AlwaysBrokenProvider()
    registry = SkillRegistry([flaky, broken])
    skills, observations = registry.snapshot()

    assert [skill.name for skill in skills] == ["recovered"]
    assert flaky.calls == 2
    assert broken.calls == 2
    by_name = {observation.provider: observation for observation in observations}
    assert by_name["flaky"].ok is True
    assert by_name["flaky"].attempts == 2
    assert by_name["broken"].ok is False
    assert by_name["broken"].attempts == 2
    assert "offline" in (by_name["broken"].error or "")


def test_registry_cache_and_invalidation() -> None:
    provider = _Provider("filesystem", [_skill("demo")])
    registry = SkillRegistry([provider], cache_ttl_seconds=60.0)
    registry.list()
    registry.list()
    assert provider.calls == 1
    registry.invalidate()
    registry.list()
    assert provider.calls == 2
    registry.snapshot()
    assert provider.calls == 2


def test_registry_applies_enabled_and_disabled_names() -> None:
    provider = _Provider("filesystem", [_skill("alpha"), _skill("beta"), _skill("gamma")])
    only_alpha = SkillRegistry([provider], enabled_names=["ALPHA"])
    assert [skill.name for skill in only_alpha.list()] == ["alpha"]

    without_beta = SkillRegistry([provider], disabled_names={"beta"})
    assert [skill.name for skill in without_beta.list()] == ["alpha", "gamma"]

    disabled_wins = SkillRegistry(
        [provider], enabled_names=["alpha", "beta"], disabled_names=["alpha"]
    )
    assert [skill.name for skill in disabled_wins.list()] == ["beta"]


def test_strict_names_reject_underscores_but_default_accepts_them(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "under_score.md").write_text("Underscore instructions", encoding="utf-8")

    default = FilesystemSkillProvider([(root, "project", 100)])
    assert [skill.name for skill in default.discover()] == ["under_score"]

    strict = FilesystemSkillProvider([(root, "project", 100)], strict_names=True)
    assert strict.discover() == []
    assert is_valid_skill_name("under_score") is True
    assert is_valid_skill_name("under_score", strict=True) is False
    with pytest.raises(ValueError, match="hyphens"):
        parse_skill_document(
            "---\nname: under_score\n---\nBody",
            fallback_name="under_score",
            source="test",
            provider="test",
            rank=1,
            strict_names=True,
        )


def test_skip_system_root_hides_vendored_directories(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for directory_name, skill_name in (
        ("ok", "ok"),
        ("node_modules", "node_modules"),
        ("__pycache__", "pycache"),
    ):
        directory = root / directory_name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: d\n---\nBody for {directory_name}",
            encoding="utf-8",
        )
    skipped = FilesystemSkillProvider(
        [SkillRoot(path=root, source="user", rank=500, skip_system=True)]
    )
    assert [skill.name for skill in skipped.discover()] == ["ok"]

    everything = FilesystemSkillProvider([SkillRoot(path=root, source="user", rank=500)])
    assert {skill.name for skill in everything.discover()} == {
        "ok",
        "node_modules",
        "pycache",
    }


def test_content_resolver_reads_through_a_custom_source(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    directory = root / "virtual"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("local copy", encoding="utf-8")

    class Resolver:
        def __init__(self) -> None:
            self.paths: list[Path] = []

        def read_text(self, path: Path, *, limit: int) -> str | None:
            self.paths.append(path)
            return "---\nname: virtual\ndescription: Remote body\n---\nResolved instructions"

        def size(self, path: Path) -> int | None:
            return None

    resolver = Resolver()
    provider = FilesystemSkillProvider([(root, "remote-workspace", 100)], resolver=resolver)
    skills = provider.discover()

    assert [skill.name for skill in skills] == ["virtual"]
    assert skills[0].content == "Resolved instructions"
    assert skills[0].source == "remote-workspace"
    assert skills[0].resource_base is None
    assert resolver.paths and resolver.paths[0].name == "SKILL.md"


def test_registry_enforces_admin_only_and_user_invocability() -> None:
    registry = SkillRegistry(
        [
            _Provider(
                "filesystem",
                [
                    _skill("admin-tool", admin_only=True),
                    _skill("user-only", user_invocable=False),
                ],
            )
        ]
    )
    assert registry.get("admin-tool") is None
    assert registry.get("admin-tool", for_admin=True) is not None
    assert registry.get("user-only") is not None
    assert registry.get("user-only", for_user=True) is None


def test_list_for_scope_filters_the_catalog() -> None:
    registry = SkillRegistry(
        [_Provider("filesystem", [_skill("alpha"), _skill("beta"), _skill("gamma")])]
    )
    child = child_scope(root_scope(), "child", granted=["beta", "gamma"], revoked=["gamma"])
    assert [skill.name for skill in registry.list_for_scope(child)] == ["beta"]
    assert [skill.name for skill in registry.list_for_scope(None)] == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_expand_accepts_slash_and_at_skill_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SkillRegistry([_Provider("filesystem", [_skill("demo", content="Do it well")])])
    monkeypatch.setattr(registry_module, "skill_registry", lambda _settings, _root: registry)
    settings = _settings()
    identity = {"tenant_id": uuid.uuid4(), "user_id": uuid.uuid4()}

    slash = expand_user_skill_invocation(
        "/demo tidy the report", settings, workspace_root=tmp_path, **identity
    )
    at_skill = expand_user_skill_invocation(
        "@skill:demo tidy the report", settings, workspace_root=tmp_path, **identity
    )
    for expanded in (slash, at_skill):
        assert expanded.startswith('<skill_content name="demo" provider="filesystem">')
        assert "Do it well" in expanded
        assert "User request:\ntidy the report" in expanded
        assert "</skill_content>" in expanded


def test_expand_preserves_unknown_and_mid_prompt_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SkillRegistry([_Provider("filesystem", [_skill("demo")])])
    monkeypatch.setattr(registry_module, "skill_registry", lambda _settings, _root: registry)
    settings = _settings()
    identity = {"tenant_id": uuid.uuid4(), "user_id": uuid.uuid4()}

    assert (
        expand_user_skill_invocation(
            "please run /demo", settings, workspace_root=tmp_path, **identity
        )
        == "please run /demo"
    )
    assert (
        expand_user_skill_invocation(
            "/unknown request", settings, workspace_root=tmp_path, **identity
        )
        == "/unknown request"
    )
    default_request = expand_user_skill_invocation(
        "/demo", settings, workspace_root=tmp_path, **identity
    )
    assert "Use the demo skill." in default_request


def test_expand_enforces_user_and_admin_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SkillRegistry(
        [
            _Provider(
                "filesystem",
                [
                    _skill("blocked", user_invocable=False),
                    _skill("admin-tool", admin_only=True),
                ],
            )
        ]
    )
    monkeypatch.setattr(registry_module, "skill_registry", lambda _settings, _root: registry)
    settings = _settings()
    identity = {"tenant_id": uuid.uuid4(), "user_id": uuid.uuid4()}

    with pytest.raises(SkillInvocationError):
        expand_user_skill_invocation("/blocked", settings, workspace_root=tmp_path, **identity)
    assert (
        expand_user_skill_invocation("/admin-tool", settings, workspace_root=tmp_path, **identity)
        == "/admin-tool"
    )
    assert "admin-tool" in expand_user_skill_invocation(
        "/admin-tool", settings, workspace_root=tmp_path, for_admin=True, **identity
    )


def test_expand_escapes_provider_attribute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = SkillRegistry(
        [
            _Provider(
                "filesystem",
                [_skill("demo", provider='bad"<&>')],
            )
        ]
    )
    monkeypatch.setattr(registry_module, "skill_registry", lambda _settings, _root: registry)
    expanded = expand_user_skill_invocation(
        "/demo",
        _settings(),
        workspace_root=tmp_path,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert 'provider="bad&quot;&lt;&amp;&gt;"' in expanded
