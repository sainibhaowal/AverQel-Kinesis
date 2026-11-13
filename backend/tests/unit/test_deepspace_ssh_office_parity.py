"""DeepSpace ssh office parity regression tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.ssh_config import (
    ExecutableLaunch,
    NodeScriptLaunch,
    SshRuntimeConfig,
    parse_ssh_runtime_config,
)
from app.deepspace.services.ssh_remote_helper import (
    MAX_FILE_BYTES,
    RPC_PROTOCOL_VERSION,
    RemoteFsError,
    _rpc_response,
    dispatch_fs_request,
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


def _remote_root(tmp_path: Path) -> Path:
    root = tmp_path / "remote"
    root.mkdir()
    return root


def _remote_root(tmp_path: Path) -> Path:
    root = tmp_path / "remote"
    root.mkdir()
    return root


def test_remote_helper_byte_reads_listing_and_safe_edits(tmp_path: Path) -> None:
    root = _remote_root(tmp_path)
    payload = b"hello remote"
    write = dispatch_fs_request(
        root,
        {
            "operation": "write_bytes",
            "path": "docs/a.bin",
            "content_base64": base64.b64encode(payload).decode(),
        },
    )
    assert write["size_bytes"] == len(payload)
    read = dispatch_fs_request(
        root, {"operation": "read_range", "path": "docs/a.bin", "offset": 1, "max_bytes": 4}
    )
    assert base64.b64decode(read["content_base64"]) == b"ello"
    listed = dispatch_fs_request(
        root, {"operation": "list", "path": "docs", "offset": 0, "limit": 10}
    )
    assert listed["entries"] == [{"name": "a.bin", "kind": "file", "size_bytes": len(payload)}]
    text_path = root / "docs" / "notes.txt"
    text_path.write_text("one\ntwo\n", encoding="utf-8")
    digest = hashlib.sha256(text_path.read_bytes()).hexdigest()
    changed = dispatch_fs_request(
        root,
        {
            "operation": "replace_text",
            "path": "docs/notes.txt",
            "expected_sha256": digest,
            "old_text": "two",
            "new_text": "three",
        },
    )
    assert changed["sha256"] == hashlib.sha256(b"one\nthree\n").hexdigest()
    patched = dispatch_fs_request(
        root,
        {
            "operation": "patch_lines",
            "path": "docs/notes.txt",
            "expected_sha256": changed["sha256"],
            "changes": [{"start_line": 2, "delete_lines": 1, "insert_lines": ["done"]}],
        },
    )
    assert patched["size_bytes"] == len(b"one\ndone\n")


def test_remote_helper_move_delete_and_path_rejections(tmp_path: Path) -> None:
    root = _remote_root(tmp_path)
    (root / "file.txt").write_text("x", encoding="utf-8")
    moved = dispatch_fs_request(
        root, {"operation": "move", "path": "file.txt", "destination": "renamed.txt"}
    )
    assert moved["destination"] == "renamed.txt"
    assert (
        dispatch_fs_request(root, {"operation": "delete", "path": "renamed.txt"})["deleted"] is True
    )
    with pytest.raises(RemoteFsError, match="inside"):
        dispatch_fs_request(root, {"operation": "stat", "path": "../outside"})
    (root / "stale.txt").write_text("old", encoding="utf-8")
    with pytest.raises(RemoteFsError, match="changed"):
        dispatch_fs_request(
            root,
            {
                "operation": "replace_text",
                "path": "stale.txt",
                "expected_sha256": "0" * 64,
                "old_text": "a",
                "new_text": "b",
            },
        )


def test_remote_helper_lstat_ranges_and_directory_validation(tmp_path: Path) -> None:
    root = _remote_root(tmp_path)
    target = root / "target.txt"
    target.write_text("text", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(target)

    link_info = dispatch_fs_request(root, {"operation": "lstat", "path": "link.txt"})
    followed = dispatch_fs_request(root, {"operation": "stat", "path": "link.txt"})
    directory = dispatch_fs_request(root, {"operation": "stat", "path": "."})
    assert link_info["kind"] == "symlink"
    assert followed["kind"] == "file"
    assert directory["kind"] == "directory"

    with pytest.raises(RemoteFsError, match="offset"):
        dispatch_fs_request(root, {"operation": "read_range", "path": "target.txt", "offset": True})
    with pytest.raises(RemoteFsError, match="byte range"):
        dispatch_fs_request(root, {"operation": "read_range", "path": "target.txt", "max_bytes": 0})
    with pytest.raises(RemoteFsError, match="not a file"):
        dispatch_fs_request(root, {"operation": "read_range", "path": "."})
    with pytest.raises(RemoteFsError, match="offset"):
        dispatch_fs_request(root, {"operation": "list", "path": ".", "offset": True})
    with pytest.raises(RemoteFsError, match="directory page"):
        dispatch_fs_request(root, {"operation": "list", "path": ".", "limit": True})
    with pytest.raises(RemoteFsError, match="not a directory"):
        dispatch_fs_request(root, {"operation": "list", "path": "target.txt"})
    with pytest.raises(RemoteFsError, match="relative remote path"):
        dispatch_fs_request(root, {"operation": "stat", "path": ""})
    with pytest.raises(RemoteFsError, match="relative remote path"):
        dispatch_fs_request(root, {"operation": "stat", "path": "bad\u0000path"})


def test_remote_helper_write_move_and_edit_fail_closed(tmp_path: Path) -> None:
    root = _remote_root(tmp_path)
    with pytest.raises(RemoteFsError, match="base64 text"):
        dispatch_fs_request(root, {"operation": "write_bytes", "path": "x", "content_base64": 4})
    with pytest.raises(RemoteFsError, match="valid base64"):
        dispatch_fs_request(
            root, {"operation": "write_bytes", "path": "x", "content_base64": "****"}
        )
    with pytest.raises(RemoteFsError, match="at most"):
        dispatch_fs_request(
            root,
            {
                "operation": "write_bytes",
                "path": "large.bin",
                "content_base64": base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode(),
            },
        )

    directory = root / "directory"
    directory.mkdir()
    with pytest.raises(RemoteFsError, match="Only files"):
        dispatch_fs_request(root, {"operation": "delete", "path": "directory"})
    with pytest.raises(FileNotFoundError, match="does not exist"):
        dispatch_fs_request(root, {"operation": "move", "path": "missing", "destination": "new"})
    with pytest.raises(RemoteFsError, match="itself"):
        dispatch_fs_request(
            root, {"operation": "move", "path": "directory", "destination": "directory/child"}
        )

    binary = root / "binary.txt"
    binary.write_bytes(b"\xff")
    digest = hashlib.sha256(b"\xff").hexdigest()
    with pytest.raises(RemoteFsError, match="UTF-8"):
        dispatch_fs_request(
            root,
            {
                "operation": "replace_text",
                "path": "binary.txt",
                "expected_sha256": digest,
                "old_text": "x",
                "new_text": "y",
            },
        )

    text = root / "text.txt"
    text.write_text("one\ntwo\n", encoding="utf-8")
    digest = hashlib.sha256(text.read_bytes()).hexdigest()
    for change in (
        [None],
        [{"start_line": True}],
        [{"start_line": 1, "delete_lines": -1}],
        [{"start_line": 9, "insert_lines": []}],
        [{"start_line": 1}, {"start_line": 1}],
        [{"start_line": 1, "delete_lines": 2}, {"start_line": 2, "delete_lines": 1}],
    ):
        with pytest.raises(RemoteFsError):
            dispatch_fs_request(
                root,
                {
                    "operation": "patch_lines",
                    "path": "text.txt",
                    "expected_sha256": digest,
                    "changes": change,
                },
            )


def test_remote_rpc_handshake_checks_protocol_and_helper_hash(tmp_path: Path) -> None:
    root = _remote_root(tmp_path)
    digest = "a" * 64
    response = _rpc_response(
        root,
        {
            "id": "1",
            "protocol_version": RPC_PROTOCOL_VERSION,
            "method": "initialize",
            "params": {"protocol_version": RPC_PROTOCOL_VERSION, "helper_sha256": digest},
        },
        digest,
    )
    assert response["result"]["helper_sha256"] == digest
    capabilities = response["result"]["capabilities"]
    assert len(capabilities) == len(set(capabilities))
    assert set(capabilities) == {
        "fs.stat",
        "fs.lstat",
        "fs.list",
        "fs.read_range",
        "fs.write_bytes",
        "fs.delete",
        "fs.move",
        "fs.replace_text",
        "fs.patch_lines",
    }
    with pytest.raises(RemoteFsError, match="version"):
        _rpc_response(root, {"id": "2", "protocol_version": 99, "method": "ping"}, digest)


def test_remote_rpc_maps_filesystem_errors_and_serves_until_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    import app.deepspace.services.ssh_remote_helper as helper_module

    root = _remote_root(tmp_path)
    digest = "a" * 64
    with pytest.raises(RemoteFsError, match="parameters"):
        _rpc_response(
            root,
            {"id": "bad", "protocol_version": RPC_PROTOCOL_VERSION, "method": "ping", "params": []},
            digest,
        )
    missing = _rpc_response(
        root,
        {
            "id": "missing",
            "protocol_version": RPC_PROTOCOL_VERSION,
            "method": "fs",
            "params": {"request": {"operation": "stat", "path": "missing"}},
        },
        digest,
    )
    assert missing["result"]["error_code"] == "SSH_NOT_FOUND"
    assert (
        _rpc_response(
            root,
            {"id": "shutdown", "protocol_version": RPC_PROTOCOL_VERSION, "method": "shutdown"},
            digest,
        )["result"]["shutdown"]
        is True
    )

    requests = [
        {"id": "ping", "protocol_version": RPC_PROTOCOL_VERSION, "method": "ping"},
        {"id": "shutdown", "protocol_version": RPC_PROTOCOL_VERSION, "method": "shutdown"},
    ]
    stdin = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in requests))
    stdout = io.StringIO()
    monkeypatch.setattr(helper_module.sys, "stdin", SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(helper_module.sys, "stdout", stdout)

    assert helper_module.serve_rpc(root, digest) == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["result"]["pong"] is True
    assert responses[1]["result"]["shutdown"] is True

    invalid_requests = [
        {"id": "unknown", "protocol_version": RPC_PROTOCOL_VERSION, "method": "unknown"},
        {"protocol_version": RPC_PROTOCOL_VERSION, "method": "ping"},
        {"id": "shutdown", "protocol_version": RPC_PROTOCOL_VERSION, "method": "shutdown"},
    ]
    stdin = io.BytesIO(
        json.dumps(invalid_requests[0]).encode()
        + b"\n"
        + json.dumps(invalid_requests[1]).encode()
        + b"\n"
        + json.dumps(invalid_requests[2]).encode()
        + b"\n"
    )
    stdout = io.StringIO()
    monkeypatch.setattr(helper_module.sys, "stdin", SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(helper_module.sys, "stdout", stdout)
    assert helper_module.serve_rpc(root, digest) == 0
    errors = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert errors[0]["id"] == "unknown"
    assert errors[0]["error"]["code"] == "SSH_REMOTE_FS_ERROR"
    assert errors[1]["id"] == ""
    assert errors[1]["error"]["code"] == "SSH_REMOTE_FS_ERROR"
    with pytest.raises(RemoteFsError, match="hash"):
        _rpc_response(
            root,
            {
                "id": "3",
                "protocol_version": 1,
                "method": "initialize",
                "params": {"protocol_version": 1, "helper_sha256": "b" * 64},
            },
            digest,
        )


def test_ssh_runtime_reads_helper_from_pyinstaller_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.ssh_runtime import SshRuntime

    frozen_root = tmp_path / "_MEI12345"
    helper = frozen_root / "app" / "deepspace" / "services" / "ssh_remote_helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"packaged helper")
    monkeypatch.setattr(sys, "_MEIPASS", str(frozen_root), raising=False)
    assert SshRuntime._read_helper_source() == b"packaged helper"


def test_ssh_runtime_builds_hash_pinned_commands_for_each_launch_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.ssh_runtime import SshRuntime, SshRuntimeError

    digest = "a" * 64
    host = SimpleNamespace(
        remote_workspace="/srv/reviewed",
        runtime_config=parse_ssh_runtime_config(
            {
                "workspace": "/srv/reviewed/project",
                "launch": {
                    "kind": "node-script",
                    "node_path": "node",
                    "bootstrap_path": "/opt/helper.mjs",
                    "bootstrap_hash": digest,
                },
            }
        ),
    )
    command, returned_digest = SshRuntime._helper_command(host)
    assert returned_digest == digest
    assert "node -e" in command
    assert "/opt/helper.mjs" in command
    assert " --rpc /srv/reviewed/project " in command

    host.runtime_config = parse_ssh_runtime_config(
        {
            "launch": {
                "kind": "executable",
                "executable_path": "/opt/helper",
                "executable_hash": digest,
            }
        }
    )
    command, returned_digest = SshRuntime._helper_command(host)
    assert returned_digest == digest
    assert "sha256sum" in command and "shasum -a 256" in command
    assert "exec /opt/helper --rpc /srv/reviewed" in command

    monkeypatch.setattr(SshRuntime, "_read_helper_source", lambda: b"local helper")
    host.runtime_config = SimpleNamespace(
        launch=None, helper_hash=hashlib.sha256(b"other").hexdigest()
    )
    with pytest.raises(SshRuntimeError, match="does not match"):
        SshRuntime._helper_command(host)

    host.runtime_config = SimpleNamespace(launch=None, helper_hash=None)
    command, returned_digest = SshRuntime._helper_command(host)
    assert returned_digest == hashlib.sha256(b"local helper").hexdigest()
    assert "python3 -c" in command


def test_office_processing_tool_obeys_office_skill_setting() -> None:
    from app.deepspace.services.tools import SETTINGS_GATED_TOOLS

    assert SETTINGS_GATED_TOOLS["office_process"] == "deepspace_bundled_office_enabled"


def test_ssh_runtime_config_validates_launch_hashes_and_sandbox_policy() -> None:
    config = parse_ssh_runtime_config({"sandbox_mode": "bubblewrap", "network_access": False})
    assert config.sandbox_mode == "bubblewrap"
    assert config.network_access is False
    with pytest.raises(ValueError, match="hash"):
        parse_ssh_runtime_config(
            {
                "launch": {
                    "kind": "executable",
                    "executable_path": "/opt/helper",
                    "executable_hash": "a" * 64,
                },
                "helper_hash": "b" * 64,
            }
        )
    with pytest.raises(ValueError, match="heartbeat"):
        SshRuntimeConfig(lease_ms=1_000, heartbeat_ms=600)


def test_ssh_runtime_config_supports_both_pinned_launch_kinds() -> None:
    digest = "a" * 64
    node = parse_ssh_runtime_config(
        {
            "launch": {
                "kind": "node-script",
                "node_path": "/usr/bin/node",
                "bootstrap_path": "/opt/deepspace/helper.mjs",
                "bootstrap_hash": digest,
            },
            "helper_hash": digest,
            "workspace": "/srv/workspace",
        }
    )
    assert isinstance(node.launch, NodeScriptLaunch)
    assert node.launch.bootstrap_path == "/opt/deepspace/helper.mjs"

    executable = parse_ssh_runtime_config(
        {
            "launch": {
                "kind": "executable",
                "executable_path": "/opt/deepspace/helper",
                "executable_hash": digest,
            }
        }
    )
    assert isinstance(executable.launch, ExecutableLaunch)
    assert parse_ssh_runtime_config(node) is node
    assert parse_ssh_runtime_config(None).launch is None


@pytest.mark.parametrize(
    "launch",
    [
        {
            "kind": "node-script",
            "node_path": "",
            "bootstrap_path": "/opt/helper.mjs",
            "bootstrap_hash": "a" * 64,
        },
        {
            "kind": "node-script",
            "bootstrap_path": "relative/helper.mjs",
            "bootstrap_hash": "a" * 64,
        },
        {
            "kind": "node-script",
            "node_path": "/usr/bin/../node",
            "bootstrap_path": "/opt/helper.mjs",
            "bootstrap_hash": "a" * 64,
        },
        {
            "kind": "node-script",
            "bootstrap_path": "/opt/helper.mjs",
            "bootstrap_hash": "A" * 64,
        },
        {
            "kind": "executable",
            "executable_path": "/opt/helper/../evil",
            "executable_hash": "a" * 64,
        },
        {
            "kind": "executable",
            "executable_path": "/opt/helper",
            "executable_hash": "bad",
        },
    ],
)
def test_ssh_runtime_config_rejects_untrusted_launch_paths_and_digests(
    launch: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        parse_ssh_runtime_config({"launch": launch})


def test_ssh_runtime_config_rejects_invalid_workspace_and_policy_fields() -> None:
    with pytest.raises(ValueError, match="absolute POSIX"):
        SshRuntimeConfig(workspace="relative")
    with pytest.raises(ValueError, match="dot segments"):
        SshRuntimeConfig(workspace="/srv/../etc")
    with pytest.raises(ValueError, match="helper hash"):
        SshRuntimeConfig(helper_hash="not-a-hash")
    with pytest.raises(ValueError):
        SshRuntimeConfig(max_pending=0)
    with pytest.raises(ValueError):
        parse_ssh_runtime_config([])


@pytest.mark.asyncio
async def test_office_render_uses_workspace_output_and_page_limit(
    tmp_path: Path, monkeypatch
) -> None:
    from app.deepspace.services.tools import office

    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-test")
    context = SimpleNamespace(
        workspace_root=tmp_path,
        settings=_settings(),
        sandbox_mode="workspace-write",
    )
    monkeypatch.setattr(office, "_binary_paths", lambda _: (None, "/usr/bin/pdftoppm"))

    async def render(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str, str]:
        prefix = Path(argv[-1])
        prefix.with_name(prefix.name + "-1.png").write_bytes(b"page1")
        prefix.with_name(prefix.name + "-2.png").write_bytes(b"page2")
        return 0, "", ""

    monkeypatch.setattr(office, "_run_office", render)
    result = await office._office_process(
        {"operation": "render_pages", "input_path": "report.pdf", "page_limit": 1},
        context,
    )
    assert result["status"] == "ok"
    assert len(result["pages"]) == 1
    assert (tmp_path / result["pages"][0]).read_bytes() == b"page1"
    assert source.read_bytes() == b"%PDF-test"


@pytest.mark.asyncio
async def test_office_export_fails_clearly_without_runtime_or_output(
    tmp_path: Path, monkeypatch
) -> None:
    from app.deepspace.services.tools import office

    (tmp_path / "source.docx").write_bytes(b"docx")
    context = SimpleNamespace(
        workspace_root=tmp_path, settings=_settings(), sandbox_mode="workspace-write"
    )
    monkeypatch.setattr(office, "_binary_paths", lambda _: (None, None))
    unavailable = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.docx"}, context
    )
    assert unavailable["status"] == "unavailable"
    monkeypatch.setattr(office, "_binary_paths", lambda _: ("/usr/bin/soffice", None))

    async def no_output(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str, str]:
        return 0, "", "no file"

    monkeypatch.setattr(office, "_run_office", no_output)
    failed = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.docx"}, context
    )
    assert failed["status"] == "error"
    assert not (tmp_path / "source.pdf").exists()


@pytest.mark.asyncio
async def test_office_tool_reports_input_runtime_and_output_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import runtime_manifest
    from app.deepspace.services.tools import office

    (tmp_path / "source.docx").write_bytes(b"docx")
    (tmp_path / "source.pdf").write_bytes(b"pdf")
    (tmp_path / "source.txt").write_text("text", encoding="utf-8")
    context = SimpleNamespace(
        workspace_root=tmp_path,
        settings=_settings(),
        sandbox_mode="workspace-write",
    )
    monkeypatch.setattr(office, "_binary_paths", lambda _: ("soffice", "pdftoppm"))

    missing = await office._office_process(
        {"operation": "export_pdf", "input_path": "missing.docx"}, context
    )
    unsupported = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.txt"}, context
    )
    pdf_export = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.pdf"}, context
    )
    unsupported_recalculation = await office._office_process(
        {"operation": "recalculate_workbook", "input_path": "source.docx"}, context
    )
    outside_output = await office._office_process(
        {
            "operation": "export_pdf",
            "input_path": "source.docx",
            "output_path": "../outside.pdf",
        },
        context,
    )
    same_input = await office._office_process(
        {
            "operation": "export_pdf",
            "input_path": "source.docx",
            "output_path": "source.docx",
            "overwrite": True,
        },
        context,
    )
    wrong_extension = await office._office_process(
        {
            "operation": "export_pdf",
            "input_path": "source.docx",
            "output_path": "output.docx",
        },
        context,
    )
    assert missing["status"] == "not_found"
    assert unsupported["status"] == "unsupported"
    assert pdf_export["status"] == "unsupported"
    assert unsupported_recalculation["status"] == "unsupported"
    assert outside_output["status"] == "refused"
    assert same_input["status"] == "refused"
    assert wrong_extension["status"] == "refused"

    monkeypatch.setattr(
        office,
        "_binary_paths",
        lambda _: (_ for _ in ()).throw(runtime_manifest.RuntimeManifestError("invalid runtime")),
    )
    invalid_runtime = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.docx"}, context
    )
    assert invalid_runtime["status"] == "unavailable"
    assert invalid_runtime["error_code"] == "RUNTIME_MANIFEST_INVALID"

    monkeypatch.setattr(office, "_binary_paths", lambda _: (None, None))
    no_office = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.docx"}, context
    )
    assert no_office["capability_failure"] is True
    assert no_office["retryable"] is False

    no_renderer = await office._office_process(
        {"operation": "render_pages", "input_path": "source.pdf"}, context
    )
    assert no_renderer["status"] == "unavailable"


@pytest.mark.asyncio
async def test_office_export_and_render_failures_do_not_leave_partial_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import office

    (tmp_path / "source.docx").write_bytes(b"docx")
    (tmp_path / "source.pdf").write_bytes(b"pdf")
    context = SimpleNamespace(
        workspace_root=tmp_path,
        settings=_settings(),
        sandbox_mode="workspace-write",
    )
    monkeypatch.setattr(office, "_binary_paths", lambda _: ("soffice", "pdftoppm"))

    async def no_pages(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str, str]:
        del argv, cwd, timeout
        return 1, "", "render failed"

    monkeypatch.setattr(office, "_run_office", no_pages)
    render_failed = await office._office_process(
        {"operation": "render_pages", "input_path": "source.pdf"}, context
    )
    assert render_failed["status"] == "error"
    assert "rendering" in render_failed["message"]
    assert not (tmp_path / "source_pages").exists()

    async def export(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str, str]:
        del timeout
        output_dir = Path(argv[argv.index("--outdir") + 1])
        (output_dir / "source.pdf").write_bytes(b"generated pdf")
        return 0, "", str(cwd)

    monkeypatch.setattr(office, "_run_office", export)
    (tmp_path / "source.pdf").unlink()
    succeeded = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.docx"}, context
    )
    assert succeeded["status"] == "ok"
    assert (tmp_path / "source.pdf").read_bytes() == b"generated pdf"


def test_office_output_paths_are_confined_and_non_overwriting(tmp_path: Path) -> None:
    from app.deepspace.services.tools import office

    context = SimpleNamespace(
        workspace_root=tmp_path,
        settings=_settings(),
        sandbox_mode="read-only",
    )
    output, error = office._output_path(context, "exports/result.pdf", must_not_exist=True)
    assert error is None
    assert output == tmp_path / "exports" / "result.pdf"

    existing = tmp_path / "existing.pdf"
    existing.write_bytes(b"pdf")
    _, error = office._output_path(context, "existing.pdf", must_not_exist=True)
    assert "already exists" in str(error)

    directory = tmp_path / "directory"
    directory.mkdir()
    _, error = office._output_path(context, "directory", must_not_exist=False)
    assert "directory" in str(error)

    _, error = office._output_path(context, "../outside.pdf", must_not_exist=True)
    assert error


@pytest.mark.asyncio
async def test_office_subprocess_collects_output_and_terminates_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import office

    class Handle:
        def __init__(self, *, timeout: bool) -> None:
            self.timeout = timeout
            self.terminated = False

        async def wait(self) -> Any:
            if self.timeout:
                raise TimeoutError
            return SimpleNamespace(exit_code=3)

        def collect_final(self, stream: str) -> dict[str, str]:
            return {"text": f"{stream} output"}

        async def terminate_and_wait(self) -> None:
            self.terminated = True

    handles: list[Handle] = []

    async def spawn(*args: Any, **kwargs: Any) -> Handle:
        del args, kwargs
        handle = Handle(timeout=bool(handles))
        handles.append(handle)
        return handle

    monkeypatch.setattr(office, "spawn_with_stdio", spawn)
    result = await office._run_office(["soffice"], cwd=tmp_path, timeout=10)
    assert result == (3, "stdout output", "stderr output")

    timed_out = await office._run_office(["soffice"], cwd=tmp_path, timeout=1)
    assert timed_out[0] == -1
    assert "time limit" in timed_out[2]
    assert handles[1].terminated is True


@pytest.mark.asyncio
async def test_office_recalculation_writes_a_separate_output(tmp_path: Path, monkeypatch) -> None:
    from app.deepspace.services.tools import office

    source = tmp_path / "book.xlsx"
    source.write_bytes(b"original")
    context = SimpleNamespace(
        workspace_root=tmp_path, settings=_settings(), sandbox_mode="workspace-write"
    )
    monkeypatch.setattr(office, "_binary_paths", lambda _: ("/usr/bin/soffice", None))

    async def recalculate(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str, str]:
        outdir = Path(argv[argv.index("--outdir") + 1])
        (outdir / "book.xlsx").write_bytes(b"recalculated")
        return 0, "", ""

    monkeypatch.setattr(office, "_run_office", recalculate)
    result = await office._office_process(
        {"operation": "recalculate_workbook", "input_path": "book.xlsx"}, context
    )
    assert result["status"] == "ok"
    assert source.read_bytes() == b"original"
    assert (tmp_path / "book_recalculated.xlsx").read_bytes() == b"recalculated"


@pytest.mark.asyncio
async def test_ssh_rpc_peer_round_trips_to_the_local_helper_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import ssh_rpc

    root = _remote_root(tmp_path)
    trust_dir = tmp_path / "trust"
    trust_dir.mkdir()
    released: list[object] = []
    trust = SimpleNamespace(
        directory=trust_dir, key_path=None, known_hosts_path=trust_dir / "known_hosts"
    )
    monkeypatch.setattr(ssh_rpc, "prepare_trust", lambda *args, **kwargs: trust)
    monkeypatch.setattr(ssh_rpc, "release_trust", lambda value: released.append(value))
    monkeypatch.setattr(
        ssh_rpc,
        "remote_environment",
        lambda: {
            "PATH": os.environ.get("PATH", ""),
            "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        },
    )
    code = (
        "from pathlib import Path; import sys; "
        "from app.deepspace.services.ssh_remote_helper import serve_rpc; "
        "raise SystemExit(serve_rpc(Path(sys.argv[1]), sys.argv[2]))"
    )
    monkeypatch.setattr(
        ssh_rpc,
        "build_ssh_argv",
        lambda *args, **kwargs: [sys.executable, "-c", code, str(root), "a" * 64],
    )
    host = SimpleNamespace(
        name="local-test", tenant_id=uuid.uuid4(), id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    peer = ssh_rpc.SshRpcPeer(
        host=host,
        key_directory=tmp_path,
        command="local test helper",
        helper_sha256="a" * 64,
        heartbeat_ms=10_000,
    )
    await peer.start()
    result = await peer.request(
        "fs",
        {
            "request": {
                "operation": "write_bytes",
                "path": "artifact.bin",
                "content_base64": base64.b64encode(b"artifact").decode(),
            }
        },
    )
    assert result["status"] == "ok"
    assert (root / "artifact.bin").read_bytes() == b"artifact"
    await peer.dispose()
    assert released == [trust]


@pytest.mark.asyncio
async def test_ssh_rpc_pool_disposes_and_evicts_peer_after_request_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import ssh_rpc

    class FailedPeer:
        def __init__(self, **_: object) -> None:
            self.active = False
            self.dispose_calls = 0

        async def start(self) -> None:
            self.active = True

        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            del method, params
            raise ssh_rpc.SshRpcError("simulated helper failure")

        async def dispose(self) -> None:
            self.dispose_calls += 1
            self.active = False

    peers: list[FailedPeer] = []

    def make_peer(**kwargs: object) -> FailedPeer:
        peer = FailedPeer(**kwargs)
        peers.append(peer)
        return peer

    monkeypatch.setattr(ssh_rpc, "SshRpcPeer", make_peer)
    host = SimpleNamespace(
        name="failed-test",
        tenant_id=uuid.uuid4(),
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        timeout_seconds=5,
    )
    pool = ssh_rpc.SshRpcPool()

    with pytest.raises(ssh_rpc.SshRpcError, match="simulated helper failure"):
        await pool.request(
            host=host,
            key_directory=tmp_path,
            command="helper",
            helper_sha256="a" * 64,
            method="fs",
            params={},
        )

    assert len(peers) == 1
    assert peers[0].dispose_calls == 1
    assert pool._peers == {}


@pytest.mark.asyncio
async def test_ssh_rpc_pool_reuses_one_peer_until_explicit_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import ssh_rpc

    class Peer:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.active = False
            self.requests: list[str] = []
            self.disposed = False

        async def start(self) -> None:
            self.active = True

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            del params
            self.requests.append(method)
            return {"status": "ok"}

        async def dispose(self) -> None:
            self.active = False
            self.disposed = True

    peers: list[Peer] = []

    def make_peer(**kwargs: Any) -> Peer:
        peer = Peer(**kwargs)
        peers.append(peer)
        return peer

    monkeypatch.setattr(ssh_rpc, "SshRpcPeer", make_peer)
    host = SimpleNamespace(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        timeout_seconds=5,
    )
    pool = ssh_rpc.SshRpcPool()
    for method in ("fs", "fs"):
        assert await pool.request(
            host=host,
            key_directory=tmp_path,
            command="helper",
            helper_sha256="a" * 64,
            method=method,
            params={},
        ) == {"status": "ok"}
    assert len(peers) == 1
    assert peers[0].requests == ["fs", "fs"]

    await pool.close_all()
    assert pool._peers == {}
    assert peers[0].disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (b"not-json\n", "invalid JSON"),
        (b'{"result":{}}\n', "request id"),
        (b'{"id":"request","result":[]}\n', "invalid result"),
        (b'{"id":"request","error":{"message":"remote denied"}}\n', "remote denied"),
        (b"x" * 1_025 + b"\n", "oversized"),
    ],
)
async def test_ssh_rpc_reader_reports_malformed_and_error_frames(
    tmp_path: Path, frame: bytes, message: str
) -> None:
    from app.deepspace.services import ssh_rpc

    class Reader:
        def __init__(self) -> None:
            self.frames = [frame, b""]

        async def readline(self) -> bytes:
            return self.frames.pop(0)

    peer = ssh_rpc.SshRpcPeer(
        host=SimpleNamespace(name="reader-test"),
        key_directory=tmp_path,
        command="helper",
        helper_sha256="a" * 64,
        max_frame_bytes=1_024,
    )
    future = asyncio.get_running_loop().create_future()
    peer._pending["request"] = future
    peer.process = SimpleNamespace(stdout=Reader(), returncode=None)

    await peer._read_responses()

    with pytest.raises(ssh_rpc.SshRpcError, match=message):
        await future


@pytest.mark.asyncio
async def test_ssh_rpc_forward_controls_are_loopback_only_and_leased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import ssh_rpc

    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Handle:
        async def wait(self) -> Any:
            return SimpleNamespace(exit_code=0)

        async def terminate_and_wait(self) -> None:
            raise AssertionError("successful forwarding should not terminate its process")

    async def spawn(argv: list[str], **kwargs: Any) -> Handle:
        calls.append((argv, kwargs))
        return Handle()

    monkeypatch.setattr(ssh_rpc, "spawn_with_stdio", spawn)
    monkeypatch.setattr(ssh_rpc, "remote_environment", lambda: {})
    trust = SimpleNamespace(key_path=tmp_path / "key", known_hosts_path=tmp_path / "known_hosts")
    host = SimpleNamespace(
        name="forward-test",
        destination="user@example.invalid",
        port=22,
        timeout_seconds=5,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
    )
    peer = ssh_rpc.SshRpcPeer(
        host=host, key_directory=tmp_path, command="helper", helper_sha256="a" * 64
    )
    peer._started = True
    peer._trust = trust
    peer._control_path = tmp_path / "control.sock"
    peer.process = SimpleNamespace(returncode=None)

    await peer.forward_tcp(
        local_port=8080, remote_host="build.example", remote_port=443, bind_address="localhost"
    )
    await peer.cancel_forward(
        local_port=8080, remote_host="build.example", remote_port=443, bind_address="localhost"
    )
    assert len(calls) == 2
    assert calls[0][0][-5:] == [
        "-O",
        "forward",
        "-L",
        "localhost:8080:build.example:443",
        "user@example.invalid",
    ]
    assert calls[1][0][-5:] == [
        "-O",
        "cancel",
        "-L",
        "localhost:8080:build.example:443",
        "user@example.invalid",
    ]

    with pytest.raises(ssh_rpc.SshRpcError, match="loopback"):
        await peer.forward_tcp(
            local_port=8080,
            remote_host="build.example",
            remote_port=443,
            bind_address="0.0.0.0",
        )
    with pytest.raises(ssh_rpc.SshRpcError, match="invalid"):
        await peer.forward_tcp(local_port=8080, remote_host="bad host", remote_port=443)


@pytest.mark.asyncio
async def test_ssh_rpc_peer_bounds_frames_and_closes_expired_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import ssh_rpc

    host = SimpleNamespace(name="lease-test")
    peer = ssh_rpc.SshRpcPeer(
        host=host,
        key_directory=tmp_path,
        command="helper",
        helper_sha256="a" * 64,
        max_frame_bytes=1_024,
        max_pending=2,
        lease_ms=1_000,
        heartbeat_ms=250,
    )
    with pytest.raises(ssh_rpc.SshRpcError, match="not active"):
        await peer.request("fs", {})
    with pytest.raises(ssh_rpc.SshRpcError, match="frame limit"):
        await peer.request("fs", {"content": "x" * 2_000}, _starting=True)
    assert peer._pending == {}
    assert peer._pending_semaphore._value == 2

    peer._last_client_activity = 0
    monkeypatch.setattr(ssh_rpc.time, "monotonic", lambda: 10.0)

    async def immediate_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(ssh_rpc.asyncio, "sleep", immediate_sleep)
    await peer._heartbeat()
    assert peer._closed is True


@pytest.mark.asyncio
async def test_ssh_rpc_heartbeat_pings_active_peer_and_recognizes_host_key_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services import ssh_rpc

    peer = ssh_rpc.SshRpcPeer(
        host=SimpleNamespace(name="heartbeat-test"),
        key_directory=tmp_path,
        command="helper",
        helper_sha256="a" * 64,
    )
    calls: list[str] = []

    async def stop_after_ping(
        method: str, params: dict[str, Any], *, _starting: bool = False
    ) -> dict[str, Any]:
        calls.append(method)
        peer._closed = True
        return {"pong": True}

    async def immediate_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr(peer, "request", stop_after_ping)
    monkeypatch.setattr(ssh_rpc.asyncio, "sleep", immediate_sleep)
    await peer._heartbeat()
    assert calls == ["ping"]

    peer.process = SimpleNamespace(returncode=255)
    peer._stderr_tail.extend(b"REMOTE HOST IDENTIFICATION HAS CHANGED")
    assert "does not match" in str(peer._connection_closed_error())
