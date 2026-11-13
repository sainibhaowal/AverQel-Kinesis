"""Behavioral tests for the KineSis SDK subprocess adapter."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.deepspace.services import subagent_provider_kinesis_sdk as kinesis_sdk
from app.deepspace.services.subprocess_stdio import SubprocessOutcome


class _FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeHandle:
    def __init__(self, stdout: bytes, stderr: bytes = b"") -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.terminated = False

    async def wait(self) -> SubprocessOutcome:
        return SubprocessOutcome(exit_code=0, signal=None)

    async def terminate_and_wait(self) -> SubprocessOutcome:
        self.terminated = True
        return SubprocessOutcome(exit_code=-15, signal="SIGTERM")


@pytest.mark.asyncio
async def test_kinesis_sdk_provider_sends_prompt_and_reads_response(monkeypatch):
    response = {
        "result": {
            "answer": "Found the file.",
            "tool_calls": [{"name": "fs_glob"}],
            "stop_reason": "completed",
            "rounds": 2,
        }
    }
    handle = _FakeHandle(json.dumps(response).encode())
    captured = {}

    async def fake_spawn(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return handle

    monkeypatch.setattr(kinesis_sdk, "spawn_with_stdio", fake_spawn)
    provider = kinesis_sdk.KineSisSdkProvider(
        command="kinesis-test",
        args=["--quiet"],
        kinesis_home="/tmp/kinesis-home",
        profile="test-profile",
        provider="local-provider",
        model="local-model",
        env={"BASE_VALUE": "provider", "KINESIS_HOME": "/wrong/home"},
        dispose_grace_ms=75,
    )

    result = await provider.run(
        kinesis_sdk.KineSisSdkRunSpec(
            task="Find the report",
            system_prompt="Use the workspace tools",
            cwd="/tmp/project",
            env={"CHILD_VALUE": "task", "KINESIS_HOME": "/also/wrong"},
        )
    )

    assert result.status == "completed"
    assert result.answer == "Found the file."
    assert result.tool_calls == [{"name": "fs_glob"}]
    assert result.rounds == 2
    assert captured["argv"] == [
        "kinesis-test",
        "--quiet",
        "run",
        "--provider",
        "local-provider",
        "--model",
        "local-model",
    ]
    assert captured["kwargs"]["cwd"] == "/tmp/project"
    assert captured["kwargs"]["env"]["BASE_VALUE"] == "provider"
    assert captured["kwargs"]["env"]["CHILD_VALUE"] == "task"
    assert captured["kwargs"]["env"]["KINESIS_HOME"] == "/tmp/kinesis-home"
    assert captured["kwargs"]["grace_ms"] == 75
    assert captured["kwargs"]["stdio"].stdin == "pipe"
    assert captured["kwargs"]["stdio"].stdout == "pipe"
    assert captured["kwargs"]["stdio"].stderr == "pipe"
    assert json.loads(handle.stdin.data.decode()) == {
        "id": json.loads(handle.stdin.data.decode())["id"],
        "method": "prompt",
        "params": {
            "prompt": "Find the report",
            "system_prompt": "Use the workspace tools",
        },
    }
    assert handle.stdin.closed
    assert not handle.terminated


@pytest.mark.asyncio
async def test_kinesis_sdk_provider_rejects_missing_home_without_spawning(monkeypatch):
    async def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("provider must validate KINESIS_HOME before spawning")

    monkeypatch.delenv("KINESIS_HOME", raising=False)
    monkeypatch.setattr(kinesis_sdk, "spawn_with_stdio", unexpected_spawn)

    result = await kinesis_sdk.KineSisSdkProvider().run(
        kinesis_sdk.KineSisSdkRunSpec(task="Read a file")
    )

    assert result.status == "failed"
    assert "KINESIS_HOME" in str(result.error)


@pytest.mark.asyncio
async def test_kinesis_sdk_provider_terminates_timed_out_child(monkeypatch):
    class HangingHandle(_FakeHandle):
        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.terminated = False

        async def wait(self) -> SubprocessOutcome:
            await asyncio.Event().wait()
            return SubprocessOutcome(exit_code=None, signal=None)

    handle = HangingHandle()

    async def fake_spawn(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(kinesis_sdk, "spawn_with_stdio", fake_spawn)
    provider = kinesis_sdk.KineSisSdkProvider(
        kinesis_home="/tmp/kinesis-home",
        initialize_timeout_ms=5,
    )

    result = await provider.run(kinesis_sdk.KineSisSdkRunSpec(task="Long task"))

    assert result.status == "failed"
    assert "timed out" in str(result.error).lower()
    assert handle.terminated


@pytest.mark.asyncio
async def test_kinesis_sdk_provider_terminates_cancelled_child(monkeypatch):
    class HangingHandle(_FakeHandle):
        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.terminated = False

        async def wait(self) -> SubprocessOutcome:
            await asyncio.Event().wait()
            return SubprocessOutcome(exit_code=None, signal=None)

    handle = HangingHandle()

    async def fake_spawn(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(kinesis_sdk, "spawn_with_stdio", fake_spawn)
    cancellation_checks = 0

    def cancel_after_start() -> bool:
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks > 1

    result = await kinesis_sdk.KineSisSdkProvider(
        kinesis_home="/tmp/kinesis-home",
        initialize_timeout_ms=10_000,
    ).run(
        kinesis_sdk.KineSisSdkRunSpec(task="Long task"),
        cancel_after_start,
    )

    assert result.status == "cancelled"
    assert handle.terminated


@pytest.mark.asyncio
async def test_kinesis_sdk_provider_reaps_child_when_run_task_is_cancelled(monkeypatch):
    class HangingHandle(_FakeHandle):
        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.terminated = False

        async def wait(self) -> SubprocessOutcome:
            await asyncio.Event().wait()
            return SubprocessOutcome(exit_code=None, signal=None)

    handle = HangingHandle()

    async def fake_spawn(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(kinesis_sdk, "spawn_with_stdio", fake_spawn)
    run_task = asyncio.create_task(
        kinesis_sdk.KineSisSdkProvider(
            kinesis_home="/tmp/kinesis-home",
            initialize_timeout_ms=10_000,
        ).run(kinesis_sdk.KineSisSdkRunSpec(task="Long task"))
    )
    await asyncio.sleep(0.01)
    run_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert handle.terminated


@pytest.mark.asyncio
async def test_kinesis_sdk_provider_rejects_per_child_tool_filters(monkeypatch):
    async def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("unsupported tool filters must fail before spawning")

    monkeypatch.setattr(kinesis_sdk, "spawn_with_stdio", unexpected_spawn)

    result = await kinesis_sdk.KineSisSdkProvider(kinesis_home="/tmp/home").run(
        kinesis_sdk.KineSisSdkRunSpec(task="Task", tool_allow=["fs_read"])
    )

    assert result.status == "failed"
    assert "tool filtering" in str(result.error)
