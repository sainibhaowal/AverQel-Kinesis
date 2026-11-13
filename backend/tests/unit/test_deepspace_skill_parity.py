"""DeepSpace skill parity regression tests."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.skill_bundle import (
    RemoteSkillProvider,
    SkillBundleError,
    verify_signed_bundle,
)
from app.deepspace.services.skill_registry import (
    BundledSkillProvider,
    FilesystemSkillProvider,
    SkillInvocationError,
    SkillInvocationPolicy,
    SkillRegistry,
    expand_user_skill_invocation,
    parse_skill_document,
)
from app.deepspace.services.tools.skill import (
    _skill_list,
    _skill_load,
    _skill_resource,
)

pytestmark = pytest.mark.unit_no_db


def _settings(**values: Any) -> SimpleNamespace:
    defaults = {
        "deepspace_spill_backend": "local",
        "deepspace_spill_local_root": "",
        "deepspace_spill_retention_seconds": 90 * 86400,
        "deepspace_spill_max_chars": 2_000_000,
        "deepspace_spill_threshold_chars": 24_000,
        "minio_bucket": "unit-test",
        "minio_endpoint": "unused.invalid",
        "deepspace_skill_custom_roots": "",
        "deepspace_bundled_skills_enabled": False,
        "deepspace_skill_user_invocation_enabled": True,
        "deepspace_permission_mode": "workspace-write",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _signed_skill_bundle() -> tuple[bytes, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    skills = [{"name": "remote-guide", "content": "Use the remote guide."}]
    canonical = json.dumps(
        skills,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = {
        "version": 1,
        "skills": skills,
        "signature": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return json.dumps(envelope).encode("utf-8"), base64.b64encode(public_key).decode("ascii")


def test_signed_remote_skills_verify_signature_and_require_https() -> None:
    raw, public_key = _signed_skill_bundle()
    bundle = verify_signed_bundle(raw, public_key)
    assert bundle.version == 1
    assert [skill.name for skill in bundle.skills] == ["remote-guide"]
    assert bundle.skills[0].provider == "remote"

    provider = RemoteSkillProvider(
        "https://skills.example.test/catalog.json",
        public_key,
        fetcher=lambda url: raw if url.endswith("catalog.json") else b"",
    )
    assert provider.discover() == list(bundle.skills)
    with pytest.raises(SkillBundleError, match="signature"):
        tampered = json.loads(raw)
        tampered["skills"][0]["content"] = "tampered instructions"
        verify_signed_bundle(json.dumps(tampered), public_key)
    with pytest.raises(SkillBundleError, match="HTTPS"):
        RemoteSkillProvider("http://skills.example.test/catalog.json", public_key)


def test_skill_frontmatter_invocation_and_ranked_override(tmp_path: Path) -> None:
    project = tmp_path / ".agents" / "skills"
    project.mkdir(parents=True)
    (project / "review.md").write_text(
        "---\nname: review\ndescription: 'Project review'\nmodel-invocable: false\nuser-invocable: true\n---\nLocal instructions",
        encoding="utf-8",
    )
    (project / "invalid.md").write_text("---\nname: Bad Name\n---\ninvalid", encoding="utf-8")
    provider = FilesystemSkillProvider([(tmp_path / ".agents" / "skills", "project", 100)])
    registry = SkillRegistry([provider])
    skill = registry.get("REVIEW", for_user=True)

    assert skill is not None
    assert skill.description == "Project review"
    assert skill.invocation == SkillInvocationPolicy(model_invocable=False, user_invocable=True)
    assert registry.get("review") is None
    assert [item.name for item in registry.list()] == ["review"]


def test_skill_frontmatter_parses_quoted_scalars_and_rejects_malformed_metadata() -> None:
    parsed = parse_skill_document(
        '---\nname: summary\ndescription: "A \\"quoted\\" skill"\n'
        "when_to_use: 'For''s analysis'\nmodel-invocable: FALSE\n"
        "ignored line without a colon\n---\n# Instructions\nDetails",
        fallback_name="fallback",
        source="test",
        provider="test",
        rank=1,
    )
    assert parsed.name == "summary"
    assert parsed.description == 'A "quoted" skill'
    assert parsed.when_to_use == "For's analysis"
    assert parsed.invocation.model_invocable is False
    assert parsed.content.startswith("# Instructions")

    malformed_quote = parse_skill_document(
        '---\nname: summary\ndescription: "invalid\\q"\n---\n',
        fallback_name="fallback",
        source="test",
        provider="test",
        rank=1,
    )
    assert malformed_quote.description == "invalid\\q"
    with pytest.raises(ValueError, match="not closed"):
        parse_skill_document(
            "---\nname: summary\n",
            fallback_name="fallback",
            source="test",
            provider="test",
            rank=1,
        )
    with pytest.raises(ValueError, match="flags"):
        parse_skill_document(
            "---\nname: summary\nuser-invocable: sometimes\n---\n",
            fallback_name="fallback",
            source="test",
            provider="test",
            rank=1,
        )


def test_skill_registry_ranks_duplicate_providers_and_contains_provider_failures() -> None:
    from app.deepspace.services.skill_registry import SkillDefinition

    preferred = SkillDefinition(
        name="Guide",
        description="project copy",
        when_to_use="",
        invocation=SkillInvocationPolicy(),
        source="project",
        provider="project",
        rank=100,
        content="project instructions",
    )
    fallback = SkillDefinition(
        name="guide",
        description="shared copy",
        when_to_use="",
        invocation=SkillInvocationPolicy(),
        source="shared",
        provider="shared",
        rank=500,
        content="shared instructions",
    )

    class Provider:
        def __init__(self, name: str, items: list[Any]) -> None:
            self.name = name
            self.items = items

        def discover(self) -> list[Any]:
            if isinstance(self.items, Exception):
                raise self.items
            return self.items

    registry = SkillRegistry(
        [
            Provider("shared", [fallback]),
            Provider("broken", RuntimeError("provider failed")),
            Provider("project", [preferred]),
        ]
    )
    assert registry.get("GUIDE").content == "project instructions"
    assert [item.name for item in registry.list()] == ["Guide"]

    unregister = registry.register_provider(Provider("extra", []))
    with pytest.raises(ValueError, match="already registered"):
        registry.register_provider(Provider("extra", []))
    unregister()
    unregister()
    assert [item.name for item in registry.list()] == ["Guide"]


def test_skill_registry_cache_uses_custom_shared_roots_and_runtime_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import skill_registry as registry_module

    workspace = tmp_path / "workspace"
    custom = tmp_path / "custom"
    shared = tmp_path / "shared"
    for root, name in (
        (workspace / ".agents" / "skills", "project"),
        (custom, "custom"),
        (shared / "skills", "shared"),
    ):
        root.mkdir(parents=True)
        (root / f"{name}.md").write_text(f"Instructions for {name}", encoding="utf-8")
    settings = SimpleNamespace(
        deepspace_skill_custom_roots=f"{custom}{os.pathsep}relative-root",
        deepspace_bundled_skills_enabled=False,
    )
    monkeypatch.setattr(registry_module, "averqel_agents_home", lambda: shared)
    monkeypatch.setattr(registry_module, "is_averqel_desktop_runtime", lambda: True)
    monkeypatch.setattr(registry_module, "_REGISTRIES", {})

    included = registry_module.skill_registry(settings, workspace, include_shared=True)
    assert included is registry_module.skill_registry(settings, workspace, include_shared=True)
    assert {item.name for item in included.list()} == {"project", "custom", "shared"}

    excluded = registry_module.skill_registry(settings, workspace, include_shared=False)
    assert excluded is not included
    assert {item.name for item in excluded.list()} == {"project", "custom"}


def test_bundled_provider_exposes_the_specialized_office_skill_set(monkeypatch) -> None:
    from app.deepspace.services import skill_registry as registry_module
    from app.deepspace.services.bundled_skills import OFFICE_SKILLS

    manager = SimpleNamespace(
        get_bundled_skill_entries=lambda: {
            name: (content, None) for name, content in OFFICE_SKILLS.items()
        }
    )
    monkeypatch.setattr(registry_module, "get_bundled_skills_manager", lambda settings: manager)
    skills = {skill.name: skill for skill in BundledSkillProvider(_settings()).discover()}

    assert {
        "office",
        "office-docx",
        "office-xlsx",
        "office-pptx",
        "office-render",
        "office-recalculate",
        "office-export-pdf",
    } <= skills.keys()
    assert all(
        skill.source == "bundled" and skill.invocation.model_invocable for skill in skills.values()
    )


def test_skill_change_notifications_and_safe_resource_reads(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "sample"
    skill_dir.mkdir(parents=True)
    document = skill_dir / "SKILL.md"
    document.write_text("Instructions", encoding="utf-8")
    resource = skill_dir / "guide.txt"
    resource.write_text("resource", encoding="utf-8")
    registry = SkillRegistry([FilesystemSkillProvider([(root, "project", 1)])])
    changes: list[object] = []
    unsubscribe = registry.subscribe(lambda event, revision: changes.append((event, revision)))
    skill = registry.get("sample")
    assert skill is not None
    registry.list()
    resource_skill = registry.get("sample")
    assert resource_skill is not None
    assert registry.read_resource(resource_skill, "guide.txt") == b"resource"
    document.write_text("Updated instructions", encoding="utf-8")
    registry.list()
    assert len(changes) == 1
    assert changes[0][0] == "skills/change"
    with pytest.raises((FileNotFoundError, PermissionError)):
        registry.read_resource(resource_skill, "../outside.txt")
    unsubscribe()


def test_skill_registry_watcher_notifies_without_a_manual_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from app.deepspace.services import skill_registry as registry_module

    monkeypatch.setattr(registry_module, "_SKILL_WATCH_INTERVAL_SECONDS", 0.01)
    root = tmp_path / "skills"
    root.mkdir()
    document = root / "first.md"
    document.write_text("First instructions", encoding="utf-8")
    registry = SkillRegistry([FilesystemSkillProvider([(root, "project", 1)])])
    assert [item.name for item in registry.list()] == ["first"]
    changed = threading.Event()
    unsubscribe = registry.subscribe(lambda event, revision: changed.set())

    document.write_text("Updated instructions", encoding="utf-8")
    assert changed.wait(timeout=1)
    watcher = registry._watch_thread
    assert watcher is not None
    unsubscribe()
    watcher.join(timeout=1)
    assert not watcher.is_alive()


def test_filesystem_skill_provider_ignores_unsafe_and_invalid_entries(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "good.md").write_text("A valid flat skill.", encoding="utf-8")
    (root / "invalid.md").write_text("---\nname: Bad Name\n---\nignored", encoding="utf-8")
    (root / "too-large.md").write_text("x" * 160_001, encoding="utf-8")
    (root / "notes.txt").write_text("not a skill", encoding="utf-8")
    (root / "empty-directory").mkdir()
    directory_skill = root / "directory-skill"
    directory_skill.mkdir()
    (directory_skill / "README.md").write_text("Directory skill.", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.md").write_text("Must not load.", encoding="utf-8")
    (root / "escaped.md").symlink_to(outside / "escaped.md")
    (root / "escaped-directory").symlink_to(outside, target_is_directory=True)
    (root / "dangling.md").symlink_to(tmp_path / "missing.md")

    skills = FilesystemSkillProvider([(root, "project", 1)]).discover()

    assert {skill.name for skill in skills} == {"good", "directory-skill"}


def test_filesystem_skill_provider_skips_missing_and_non_directory_roots(tmp_path: Path) -> None:
    file_root = tmp_path / "not-a-directory"
    file_root.write_text("file", encoding="utf-8")
    provider = FilesystemSkillProvider(
        [(tmp_path / "missing", "missing", 1), (file_root, "file", 2)]
    )

    assert provider.discover() == []


def test_skill_registry_continues_after_listener_and_skill_file_failures(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    document = root / "guide.md"
    document.write_text("Initial guide.", encoding="utf-8")
    provider = FilesystemSkillProvider([(root, "project", 1)])
    registry = SkillRegistry([provider])
    registry.list()
    changes: list[str] = []

    def broken_listener(event: str, revision: Any) -> None:
        del revision
        changes.append(event)
        raise RuntimeError("listener failed")

    unsubscribe = registry.subscribe(broken_listener)
    document.write_text("Updated guide.", encoding="utf-8")
    assert registry.list()[0].content == "Updated guide."
    assert changes == ["skills/change"]
    unsubscribe()
    watcher = registry._watch_thread
    if watcher is not None:
        watcher.join(timeout=1)

    class MissingFileProvider:
        name = "vanishing"

        def discover(self) -> list[Any]:
            return [
                parse_skill_document(
                    "Visible content",
                    fallback_name="vanishing",
                    source="test",
                    provider=self.name,
                    rank=1,
                    path=root / "removed.md",
                )
            ]

    assert SkillRegistry([MissingFileProvider()]).list()[0].name == "vanishing"


def test_skill_registry_resource_read_rejects_missing_base_escape_and_oversize(
    tmp_path: Path,
) -> None:
    from app.deepspace.services.skill_registry import SkillDefinition

    registry = SkillRegistry()
    without_resources = SkillDefinition(
        name="plain",
        description="",
        when_to_use="",
        invocation=SkillInvocationPolicy(),
        source="test",
        provider="test",
        rank=1,
        content="instructions",
    )
    with pytest.raises(FileNotFoundError, match="no resource directory"):
        registry.read_resource(without_resources, "guide.txt")

    root = tmp_path / "skill"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "linked.txt").symlink_to(outside)
    (root / "large.bin").write_bytes(b"x" * 160_001)
    skill = SkillDefinition(
        name="resources",
        description="",
        when_to_use="",
        invocation=SkillInvocationPolicy(),
        source="test",
        provider="test",
        rank=1,
        content="instructions",
        resource_base=root,
    )
    with pytest.raises(PermissionError, match="outside"):
        registry.read_resource(skill, "../outside.txt")
    with pytest.raises(PermissionError, match="outside"):
        registry.read_resource(skill, "linked.txt")
    with pytest.raises(ValueError, match="read limit"):
        registry.read_resource(skill, "large.bin")


@pytest.mark.asyncio
async def test_skill_tools_load_list_and_read_text_and_binary_resources(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".agents" / "skills" / "resource-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: resource-skill\ndescription: Resource access\n---\nFollow this.\n",
        encoding="utf-8",
    )
    (skill_dir / "guide.txt").write_text("resource text", encoding="utf-8")
    (skill_dir / "payload.bin").write_bytes(b"\x00\xff")
    context = SimpleNamespace(
        workspace_root=workspace,
        settings=_settings(),
        auth=SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
    )

    listing = await _skill_list({}, context)
    loaded = await _skill_load({"name": "resource-skill"}, context)
    text_resource = await _skill_resource({"name": "resource-skill", "path": "guide.txt"}, context)
    binary_resource = await _skill_resource(
        {"name": "resource-skill", "path": "payload.bin"}, context
    )
    refused = await _skill_resource({"name": "resource-skill", "path": "../../secret.txt"}, context)
    missing = await _skill_resource({"name": "missing", "path": "guide.txt"}, context)

    assert listing["skill_count"] == 1
    assert listing["skills"][0]["name"] == "resource-skill"
    assert loaded["status"] == "ok" and loaded["instructions"] == "Follow this."
    assert text_resource["encoding"] == "utf-8"
    assert text_resource["content"] == "resource text"
    assert binary_resource["encoding"] == "base64"
    assert base64.b64decode(binary_resource["content"]) == b"\x00\xff"
    assert refused["status"] == "error"
    assert missing["status"] == "not_found"


@pytest.mark.asyncio
async def test_skill_tools_resolve_default_workspace_and_bound_loaded_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import skill as skill_tools

    workspace = tmp_path / "default-workspace"
    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    monkeypatch.setattr(
        skill_tools,
        "workspace_root_for",
        lambda settings, tenant_id, user_id, workspace_id=None: workspace,
    )
    context = SimpleNamespace(
        workspace_root=None,
        settings=_settings(),
        auth=auth,
        conversation_id=uuid.uuid4(),
    )

    empty = await skill_tools._skill_list({}, context)
    assert empty["status"] == "ok" and empty["skill_count"] == 0

    skill_dir = workspace / ".agents" / "skills" / "long-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("x" * (skill_tools.MAX_SKILL_CHARS + 1), encoding="utf-8")
    loaded = await skill_tools._skill_load({"name": "long-skill"}, context)

    assert loaded["status"] == "ok"
    assert loaded["path"] == ".agents/skills/long-skill/SKILL.md"
    assert len(loaded["instructions"]) == skill_tools.MAX_SKILL_CHARS
    assert loaded["truncated"] is True


def test_skill_tool_telemetry_failure_does_not_break_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.tools import skill as skill_tools

    def fail_recording(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(skill_tools, "record_skill_event", fail_recording)
    skill_tools._skill_event(
        SimpleNamespace(settings=None),
        action="list",
        name="",
    )


def test_workspace_skill_filtering_respects_invocation_query_limit_and_external_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import skill_registry as registry_module
    from app.deepspace.services.tools.skill import workspace_skills

    workspace = tmp_path / "workspace"
    skills_root = workspace / ".agents" / "skills"
    user_skill = skills_root / "user-guide"
    internal_skill = skills_root / "internal-guide"
    user_skill.mkdir(parents=True)
    internal_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        "---\nname: user-guide\ndescription: Build reports\nwhen-to-use: reporting\n---\nUser skill",
        encoding="utf-8",
    )
    (internal_skill / "SKILL.md").write_text(
        "---\nname: internal-guide\nuser-invocable: false\n---\nInternal skill",
        encoding="utf-8",
    )
    external = tmp_path / "custom-skills"
    external.mkdir()
    (external / "outside.md").write_text("External skill", encoding="utf-8")
    settings = _settings(deepspace_skill_custom_roots=str(external))
    monkeypatch.setattr(registry_module, "_REGISTRIES", {})
    owner = uuid.uuid4()

    visible = workspace_skills(
        settings,
        tenant_id=owner,
        user_id=owner,
        workspace_root=workspace,
        include_shared=False,
        for_user=True,
    )
    filtered = workspace_skills(
        settings,
        tenant_id=owner,
        user_id=owner,
        workspace_root=workspace,
        query="report",
        limit=0,
        include_shared=False,
    )

    assert [item["name"] for item in visible] == ["outside", "user-guide"]
    assert len(filtered) == 1 and filtered[0]["name"] == "user-guide"
    external_item = next(item for item in visible if item["name"] == "outside")
    assert external_item["path"]


@pytest.mark.asyncio
async def test_remote_filesystem_tool_surface_covers_all_supported_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.tools import ssh_fs

    class Runtime:
        async def read_file(self, host: object, path: str) -> Any:
            return SimpleNamespace(
                content="x" * (ssh_fs.MAX_READ_CHARS + 1),
                path=path,
                size_bytes=ssh_fs.MAX_READ_CHARS + 1,
                sha256="a" * 64,
            )

        async def write_file(self, host: object, path: str, content: str) -> Any:
            return SimpleNamespace(path=path, size_bytes=len(content))

        async def list_directory_page(
            self, host: object, path: str, *, offset: int, limit: int
        ) -> dict[str, Any]:
            return {
                "path": path,
                "offset": offset,
                "next_offset": None,
                "total_entries": 1,
                "truncated": False,
                "entries": [SimpleNamespace(name="file.txt", kind="file", size_bytes=4)],
            }

        async def delete_file(self, host: object, path: str) -> str:
            return path

        async def stat_path(self, host: object, path: str, *, follow_symlinks: bool) -> Any:
            return SimpleNamespace(path=path, kind="file", size_bytes=4)

        async def read_range(
            self, host: object, path: str, *, offset: int, max_bytes: int
        ) -> dict[str, Any]:
            data = b"hello"[:max_bytes]
            return {
                "path": path,
                "offset": offset,
                "total_bytes": 5,
                "content": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "next_offset": offset + len(data),
            }

        async def write_bytes(self, host: object, path: str, content: bytes) -> Any:
            return SimpleNamespace(path=path, size_bytes=len(content))

        async def move_file(self, host: object, path: str, destination: str) -> tuple[str, str]:
            return path, destination

        async def edit_file(self, host: object, path: str, **kwargs: Any) -> dict[str, Any]:
            return {"path": path, "sha256": "b" * 64}

        async def patch_file(self, host: object, path: str, **kwargs: Any) -> dict[str, Any]:
            return {"path": path, "sha256": "c" * 64}

    monkeypatch.setattr(ssh_fs, "_runtime", lambda context: Runtime())
    monkeypatch.setattr(ssh_fs, "resolve_host", lambda context, name: name)
    context = SimpleNamespace(settings=SimpleNamespace(deepspace_ssh_runtime_enabled=True))
    arguments = {"host": "reviewed", "path": "file.txt"}

    read = await ssh_fs._ssh_fs_read(arguments, context)
    written = await ssh_fs._ssh_fs_write({**arguments, "content": "text"}, context)
    listed = await ssh_fs._ssh_fs_list({**arguments, "offset": 0, "limit": 5}, context)
    deleted = await ssh_fs._ssh_fs_delete(arguments, context)
    stated = await ssh_fs._ssh_fs_stat(arguments, context)
    ranged = await ssh_fs._ssh_fs_read_range({**arguments, "offset": 0, "max_bytes": 5}, context)
    text = await ssh_fs._ssh_fs_read_text({**arguments, "offset": 0, "max_bytes": 5}, context)
    bytes_written = await ssh_fs._ssh_fs_write_bytes(
        {**arguments, "content_base64": base64.b64encode(b"data").decode()}, context
    )
    moved = await ssh_fs._ssh_fs_move({**arguments, "destination": "renamed.txt"}, context)
    edited = await ssh_fs._ssh_fs_edit(
        {
            **arguments,
            "expected_sha256": "a" * 64,
            "old_text": "old",
            "new_text": "new",
        },
        context,
    )
    patched = await ssh_fs._ssh_fs_patch(
        {
            **arguments,
            "expected_sha256": "a" * 64,
            "changes": [{"start_line": 1, "delete_lines": 1, "insert_lines": ["new"]}],
        },
        context,
    )

    assert read["status"] == "ok" and read["truncated"] is True
    assert written["size_bytes"] == 4
    assert listed["entries"][0]["name"] == "file.txt"
    assert deleted["deleted"] is True
    assert stated["kind"] == "file"
    assert base64.b64decode(ranged["content_base64"]) == b"hello"
    assert text["content"] == "hello"
    assert bytes_written["size_bytes"] == 4
    assert moved["destination"] == "renamed.txt"
    assert edited["sha256"] == "b" * 64
    assert patched["sha256"] == "c" * 64


@pytest.mark.asyncio
async def test_remote_text_chunks_preserve_utf8_boundaries_and_refuse_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.tools import ssh_fs

    context = SimpleNamespace(settings=SimpleNamespace(deepspace_ssh_runtime_enabled=True))

    async def chunk(data: bytes) -> dict[str, Any]:
        async def read_range(arguments: dict[str, Any], tool_context: Any) -> dict[str, Any]:
            del arguments, tool_context
            return {
                "status": "ok",
                "path": "file.txt",
                "offset": 4,
                "total_bytes": 32,
                "content_base64": base64.b64encode(data).decode(),
                "next_offset": 4 + len(data),
            }

        monkeypatch.setattr(ssh_fs, "_ssh_fs_read_range", read_range)
        return await ssh_fs._ssh_fs_read_text({"offset": 4}, context)

    partial_codepoint = await chunk(b"\x82\xachello\xe2\x82")
    assert partial_codepoint["content"] == "hello"
    assert partial_codepoint["offset"] == 6
    assert partial_codepoint["next_offset"] == 11

    binary = await chunk(b"valid\xfftext")
    assert binary["status"] == "binary"
    assert "ssh_fs_read_range" in binary["message"]


@pytest.mark.asyncio
async def test_remote_filesystem_tools_short_circuit_when_runtime_is_disabled() -> None:
    from app.deepspace.services.tools import ssh_fs

    context = SimpleNamespace(settings=SimpleNamespace(deepspace_ssh_runtime_enabled=False))
    arguments = {"host": "reviewed", "path": "file.txt"}
    calls = (
        (ssh_fs._ssh_fs_read, arguments),
        (ssh_fs._ssh_fs_write, {**arguments, "content": "text"}),
        (ssh_fs._ssh_fs_list, arguments),
        (ssh_fs._ssh_fs_delete, arguments),
        (ssh_fs._ssh_fs_stat, arguments),
        (ssh_fs._ssh_fs_read_range, arguments),
        (ssh_fs._ssh_fs_read_text, arguments),
        (ssh_fs._ssh_fs_write_bytes, {**arguments, "content_base64": "ZGF0YQ=="}),
        (ssh_fs._ssh_fs_move, {**arguments, "destination": "new.txt"}),
        (
            ssh_fs._ssh_fs_edit,
            {**arguments, "expected_sha256": "a" * 64, "old_text": "a", "new_text": "b"},
        ),
        (
            ssh_fs._ssh_fs_patch,
            {
                **arguments,
                "expected_sha256": "a" * 64,
                "changes": [{"start_line": 1, "delete_lines": 1, "insert_lines": ["b"]}],
            },
        ),
    )

    results = [await handler(args, context) for handler, args in calls]

    assert len(results) == len(calls)
    assert all(result == results[0] for result in results)
    assert results[0]["status"] == "error"


@pytest.mark.asyncio
async def test_remote_filesystem_tools_convert_runtime_failures_to_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services import ssh_runtime
    from app.deepspace.services.tools import ssh_fs

    class BrokenRuntime:
        def __getattr__(self, name: str) -> Any:
            async def fail(*args: Any, **kwargs: Any) -> Any:
                raise ssh_runtime.SshRuntimeError(f"{name} unavailable")

            return fail

    monkeypatch.setattr(ssh_fs, "resolve_host", lambda context, name: name)
    monkeypatch.setattr(ssh_fs, "_runtime", lambda context: BrokenRuntime())
    context = SimpleNamespace(settings=SimpleNamespace(deepspace_ssh_runtime_enabled=True))
    arguments = {"host": "reviewed", "path": "file.txt"}
    calls = (
        (ssh_fs._ssh_fs_read, arguments),
        (ssh_fs._ssh_fs_write, {**arguments, "content": "text"}),
        (ssh_fs._ssh_fs_list, arguments),
        (ssh_fs._ssh_fs_delete, arguments),
        (ssh_fs._ssh_fs_stat, arguments),
        (ssh_fs._ssh_fs_read_range, arguments),
        (ssh_fs._ssh_fs_read_text, arguments),
        (ssh_fs._ssh_fs_write_bytes, {**arguments, "content_base64": "ZGF0YQ=="}),
        (ssh_fs._ssh_fs_move, {**arguments, "destination": "new.txt"}),
        (
            ssh_fs._ssh_fs_edit,
            {**arguments, "expected_sha256": "a" * 64, "old_text": "a", "new_text": "b"},
        ),
        (
            ssh_fs._ssh_fs_patch,
            {
                **arguments,
                "expected_sha256": "a" * 64,
                "changes": [{"start_line": 1, "delete_lines": 1, "insert_lines": ["b"]}],
            },
        ),
    )

    results = [await handler(args, context) for handler, args in calls]

    assert all(result["status"] == "error" for result in results)
    assert all("unavailable" in result["message"] for result in results)


@pytest.mark.asyncio
async def test_remote_filesystem_tools_map_host_and_trust_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.ssh import SshError, SshHostNotFoundError, SshTrustError
    from app.deepspace.services.ssh_runtime import RemoteNotFoundError, RemotePathRefusedError
    from app.deepspace.services.tools import ssh_fs

    active_error: Exception = RuntimeError("unavailable")

    class BrokenRuntime:
        def __getattr__(self, name: str) -> Any:
            async def fail(*args: Any, **kwargs: Any) -> Any:
                raise active_error

            return fail

    monkeypatch.setattr(ssh_fs, "resolve_host", lambda context, name: name)
    monkeypatch.setattr(ssh_fs, "_runtime", lambda context: BrokenRuntime())
    context = SimpleNamespace(settings=SimpleNamespace(deepspace_ssh_runtime_enabled=True))
    arguments = {"host": "reviewed", "path": "file.txt"}
    calls = (
        (ssh_fs._ssh_fs_read, arguments),
        (ssh_fs._ssh_fs_write, {**arguments, "content": "text"}),
        (ssh_fs._ssh_fs_list, arguments),
        (ssh_fs._ssh_fs_delete, arguments),
    )
    failures = (
        (SshHostNotFoundError("unknown host"), None),
        (SshTrustError("untrusted key"), "SSH_HOST_UNTRUSTED"),
        (RemotePathRefusedError("outside reviewed root"), "SSH_PATH_REFUSED"),
        (RemoteNotFoundError("missing path"), "SSH_NOT_FOUND"),
        (SshError("ssh failed"), "SSH_COMMAND_FAILED"),
        (RuntimeError("unexpected"), None),
    )

    for handler, tool_arguments in calls:
        for active_error, expected_code in failures:
            result = await handler(tool_arguments, context)
            assert result["status"] == "error"
            assert result["message"] == str(active_error)
            if isinstance(active_error, RemoteNotFoundError) and handler is ssh_fs._ssh_fs_write:
                assert result["error_code"] == "SSH_COMMAND_FAILED"
            elif expected_code is not None:
                assert result["error_code"] == expected_code


@pytest.mark.asyncio
async def test_remote_binary_write_rejects_malformed_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.deepspace.services.tools import ssh_fs

    monkeypatch.setattr(ssh_fs, "resolve_host", lambda context, name: name)
    context = SimpleNamespace(settings=SimpleNamespace(deepspace_ssh_runtime_enabled=True))
    result = await ssh_fs._ssh_fs_write_bytes(
        {"host": "reviewed", "path": "file.bin", "content_base64": "%%%"}, context
    )

    assert result["status"] == "error"
    assert "base64" in result["message"]


def test_user_skill_invocation_expands_only_user_enabled_skill(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".agents" / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\nuser-invocable: true\n---\nUse concise bullets.",
        encoding="utf-8",
    )
    settings = _settings()
    monkeypatch.delenv("AVERQEL_DESKTOP_LOCAL_RUNTIME", raising=False)
    monkeypatch.delenv("AVERQEL_DESKTOP_PACKAGED", raising=False)
    expanded = expand_user_skill_invocation(
        "/summarize explain this",
        settings,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        workspace_root=workspace,
    )
    assert "Use concise bullets." in expanded
    assert "User request:\nexplain this" in expanded
    assert (
        expand_user_skill_invocation(
            "ordinary request",
            settings,
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            workspace_root=workspace,
        )
        == "ordinary request"
    )


def test_user_invocation_refuses_skill_disallowed_for_users(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".agents" / "skills" / "internal"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: internal\nuser-invocable: false\n---\nInternal only",
        encoding="utf-8",
    )
    with pytest.raises(SkillInvocationError):
        expand_user_skill_invocation(
            "/internal",
            _settings(),
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            workspace_root=workspace,
        )
