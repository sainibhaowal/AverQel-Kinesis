"""The extension jail, proved on the host that runs it.

An extension that carries code must not run with the server's own rights.
These tests check that the handler runs in **its own process**, inside the
deployment's cage, with no deployment secret in its environment, and that it
is stopped when it runs past its budget.

A host that offers no cage has no cage, so the call is refused rather than
run unguarded.  Those checks skip with the reason on such a host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.deepspace.services.plugins import (
    PluginError,
    call_plugin_tool,
    parse_manifest,
    plugin_code_allowed,
)

pytestmark = pytest.mark.unit_no_db

#: A handler that reports what it can see.
REPORTER = """
import json
import os
import sys

def report(arguments):
    return {
        "status": "ok",
        "pid": os.getpid(),
        "environment": dict(os.environ),
        "cwd": os.getcwd(),
    }
"""


def _manifest(tmp_path: Path, **overrides: Any) -> Any:
    """Build one plugin manifest for a test.

    Args:
        tmp_path: The directory the test works in.
        **overrides: The manifest fields to override.

    Returns:
        The manifest.
    """
    values: dict[str, Any] = {
        "id": "reporter",
        "name": "Reporter",
        "version": "1.0.0",
        "description": "Reports what it can see.",
        "source": "workspace",
        "handler": "report",
        "handler_module": "reporter.py",
        "handler_kind": "python",
        "handler_timeout_seconds": 30,
        "tools": [
            {
                "name": "report",
                "description": "Reports what the handler can see.",
                "mode": "read",
            }
        ],
    }
    values.update(overrides)
    return parse_manifest(values, source="workspace")


def _settings(root: Path | None = None) -> Any:
    """Return the deployment settings, with a workspace base when given.

    Args:
        root: The base the conversation's workspace lives under.

    Returns:
        The settings.
    """
    from app.core.config import get_settings

    base = get_settings()
    if root is None:
        return base
    return base.model_copy(update={"deepspace_workspace_root": str(root)})


def _cage_available() -> bool:
    """Report whether this host can run a cage.

    Returns:
        True when the cage works.
    """
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(  # noqa: S603
        [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            "/",
            "/",
            "--",
            "/bin/true",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return probe.returncode == 0


def test_a_workspace_handler_runs_in_its_own_process(tmp_path: Path) -> None:
    """The handler does not share a process with the server."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    (workspace / "reporter.py").write_text(REPORTER, encoding="utf-8")
    manifest = _manifest(tmp_path)

    result = call_plugin_tool(
        manifest,
        tool_name="report",
        arguments={},
        context=_context(workspace),
    )

    assert result["status"] == "ok"
    assert int(result["pid"]) != os.getpid()


def test_a_workspace_handler_never_sees_a_deployment_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler's environment carries no deployment secret."""
    monkeypatch.setenv("AKS_PROVIDER_SECRET_KEYRING_JSON", "super-secret")
    monkeypatch.setenv("AKS_JWT_SECRET", "super-secret")
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    (workspace / "reporter.py").write_text(REPORTER, encoding="utf-8")
    manifest = _manifest(tmp_path)

    result = call_plugin_tool(
        manifest,
        tool_name="report",
        arguments={},
        context=_context(workspace),
    )

    environment = {str(name): str(value) for name, value in result["environment"].items()}
    assert "AKS_PROVIDER_SECRET_KEYRING_JSON" not in environment
    assert "AKS_JWT_SECRET" not in environment
    assert environment.get("DEEPSPACE_PLUGIN") == "1"


@pytest.mark.skipif(not _cage_available(), reason="This host cannot create the namespaces.")
def test_a_workspace_handler_cannot_write_outside_the_workspace(tmp_path: Path) -> None:
    """The cage keeps the handler's write inside the workspace."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    (workspace / "reporter.py").write_text(
        "import json, os\n"
        "def report(arguments):\n"
        f"    with open({str(outside)!r}, 'w') as handle:\n"
        "        handle.write('escaped')\n"
        "    return {'status': 'ok', 'wrote': True}\n",
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path)

    # The cage gives the handler its own temporary filesystem, so the write
    # lands inside the cage and the host never sees the file.
    result = call_plugin_tool(
        manifest,
        tool_name="report",
        arguments={},
        context=_context(workspace),
    )

    assert result["status"] == "ok"
    assert not outside.exists()


def test_a_workspace_handler_that_hangs_is_stopped(tmp_path: Path) -> None:
    """A handler that runs past its budget is stopped."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    (workspace / "reporter.py").write_text(
        "import time\ndef report(arguments):\n    time.sleep(120)\n    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path, handler_timeout_seconds=2)

    with pytest.raises(PluginError):
        call_plugin_tool(
            manifest,
            tool_name="report",
            arguments={},
            context=_context(workspace),
        )


def test_a_workspace_handler_that_raises_fails_the_call(tmp_path: Path) -> None:
    """A handler that raises does not take the server down."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    (workspace / "reporter.py").write_text(
        "def report(arguments):\n    raise ValueError('the handler failed')\n",
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path)

    with pytest.raises(PluginError):
        call_plugin_tool(
            manifest,
            tool_name="report",
            arguments={},
            context=_context(workspace),
        )


def test_a_deployment_that_refuses_extension_code_refuses_the_call(tmp_path: Path) -> None:
    """The deployment's switch stops extension code before it runs."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    (workspace / "reporter.py").write_text(REPORTER, encoding="utf-8")
    manifest = _manifest(tmp_path)

    class _Settings:
        deepspace_plugin_code_enabled = False

    assert plugin_code_allowed(manifest, _Settings()) is False

    with pytest.raises(PluginError):
        call_plugin_tool(
            manifest,
            tool_name="report",
            arguments={},
            context=_context(workspace, settings=_Settings()),
        )


def test_a_handler_that_leaves_the_workspace_is_refused(tmp_path: Path) -> None:
    """A handler file outside the workspace never runs."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    outside = tmp_path / "reporter.py"
    outside.write_text(REPORTER, encoding="utf-8")

    with pytest.raises(PluginError):
        _manifest(tmp_path, handler_module="../reporter.py")


@pytest.mark.skipif(not _cage_available(), reason="This host cannot create the namespaces.")
def test_a_command_handler_runs_in_the_cage(tmp_path: Path) -> None:
    """A command handler writes in the workspace and nowhere else."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    manifest = _manifest(
        tmp_path,
        handler="printf hello > inside.txt; printf hello > " + str(outside),
        handler_module=None,
        handler_kind="command",
    )

    result = call_plugin_tool(
        manifest,
        tool_name="report",
        arguments={},
        context=_context(workspace),
    )

    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "hello"
    assert not outside.exists()
    assert result["status"] in {"ok", "error"}


def _context(workspace: Path, *, settings: Any = None, sandbox_mode: str = "") -> Any:
    """Build the tool context a plugin call receives.

    Args:
        workspace: The workspace the call runs in.
        settings: The deployment settings.
        sandbox_mode: The conversation's own containment mode, when it has
            one.

    Returns:
        The context.
    """
    if settings is None:
        settings = _settings(workspace.parent.parent)
    from types import SimpleNamespace

    return SimpleNamespace(
        settings=settings or _settings(),
        auth=SimpleNamespace(tenant_id="tenant", user_id="user"),
        conversation_id="conversation",
        db=None,
        workspace_root=workspace,
        sandbox_mode=sandbox_mode,
    )


@pytest.mark.skipif(not _cage_available(), reason="This host cannot create the namespaces.")
def test_a_read_only_conversation_cannot_write_in_the_workspace(tmp_path: Path) -> None:
    """The conversation's own containment mode governs the extension."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    target = workspace / "written.txt"
    (workspace / "reporter.py").write_text(
        "def report(arguments):\n"
        f"    open({str(target)!r}, 'w').write('x')\n"
        "    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path)

    with pytest.raises(PluginError):
        call_plugin_tool(
            manifest,
            tool_name="report",
            arguments={},
            context=_context(workspace, sandbox_mode="read-only"),
        )

    assert not target.exists()


def test_a_full_access_conversation_still_runs_in_its_own_process(tmp_path: Path) -> None:
    """Full access drops the cage, never the process boundary."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    (workspace / "reporter.py").write_text(REPORTER, encoding="utf-8")
    manifest = _manifest(tmp_path)

    result = call_plugin_tool(
        manifest,
        tool_name="report",
        arguments={},
        context=_context(workspace, sandbox_mode="danger-full-access"),
    )

    assert result["status"] == "ok"
    assert int(result["pid"]) != os.getpid()


def test_the_handler_receives_its_arguments(tmp_path: Path) -> None:
    """The handler is called with the tool's arguments."""
    workspace = tmp_path / "tenant" / "user"
    workspace.mkdir(parents=True)
    (workspace / "reporter.py").write_text(
        "def report(arguments):\n    return {'status': 'ok', 'seen': arguments}\n",
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path)

    result = call_plugin_tool(
        manifest,
        tool_name="report",
        arguments={"value": 7},
        context=_context(workspace),
    )

    assert result["seen"]["value"] == 7
    assert json.dumps(result["seen"])
