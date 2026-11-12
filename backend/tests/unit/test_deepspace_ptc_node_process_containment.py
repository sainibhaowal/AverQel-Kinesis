from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.deepspace.services import ptc_runtime_node
from app.deepspace.services.ptc_runtime_node import NodePtcRuntime, PtcRunSpec


class _EmptyPipe:
    async def readline(self) -> bytes:
        return b""

    async def read(self, _size: int) -> bytes:
        return b""


class _ExitedProcess:
    pid = 123456
    returncode: int | None = None
    stdin = None
    stdout = _EmptyPipe()
    stderr = _EmptyPipe()

    async def wait(self) -> int:
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_node_ptc_launches_and_releases_its_managed_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = _ExitedProcess()
    spawn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    managed_range = Mock()

    class FakeHandle:
        def __init__(self) -> None:
            self.process = process

        async def wait(self) -> object:
            result = await process.wait()
            managed_range.mark_stopped()
            return SimpleNamespace(exit_code=result, signal=None)

        async def terminate_and_wait(self) -> object:
            return await self.wait()

    class FakeSandbox:
        def available(self) -> bool:
            return True

        def wrap(self, argv: list[str], **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(argv=["sandbox", *argv], backend="fake", sandboxed=True)

    async def spawn(*argv: object, **kwargs: object) -> FakeHandle:
        spawn_calls.append((argv, kwargs))
        return FakeHandle()

    monkeypatch.setattr(ptc_runtime_node.shutil, "which", lambda _command: "/usr/bin/node")
    monkeypatch.setattr(ptc_runtime_node, "BwrapBackend", FakeSandbox)
    monkeypatch.setattr(ptc_runtime_node, "spawn_with_stdio", spawn)

    runtime = NodePtcRuntime(SimpleNamespace())
    spec = PtcRunSpec(
        program="return 1;",
        bindings={},
        cwd=str(workspace),
        timeout_ms=1000,
        sandbox_policy="workspace-write",
        max_tool_calls=2,
    )

    outcome = await runtime.run(spec)

    assert outcome.status == "ok"
    assert len(spawn_calls) == 1
    child_argv = spawn_calls[0][0][0]
    assert child_argv[0] == "sandbox"
    assert len(child_argv) == 3
    assert spawn_calls[0][1]["cwd"] == str(workspace)
    assert spawn_calls[0][1]["stdio"].stdin == "pipe"
    assert spawn_calls[0][1]["stdio"].stdout == "pipe"
    assert spawn_calls[0][1]["stdio"].stderr == "pipe"
    assert spawn_calls[0][1]["stream_limit"] == runtime._config.max_message_bytes
    managed_range.mark_stopped.assert_called_once_with()
