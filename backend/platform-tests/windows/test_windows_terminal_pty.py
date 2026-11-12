from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from app.deepspace.services.terminals import ManagedProcess, build_session_environment


@pytest.mark.asyncio
async def test_native_windows_terminal_pty_round_trips_input_and_releases_job(
    tmp_path: Path,
) -> None:
    assert sys.platform == "win32"
    env = build_session_environment(workspace=tmp_path, shell="cmd")
    assert "SYSTEMROOT" in env
    managed = await ManagedProcess.start(
        argv=[
            sys.executable,
            "-c",
            "import sys; print('PTY_READY', flush=True); "
            "value = input(); print('PTY_ECHO:' + value, flush=True)",
        ],
        shell="cmd",
        cwd=tmp_path,
        env=env,
        rows=24,
        columns=80,
    )
    process_range = managed.containment_range
    try:
        assert managed.pty is True
        assert process_range is not None
        assert process_range.platform == "windows"

        deadline = time.monotonic() + 30
        while "PTY_READY" not in managed.peek(65_536):
            if managed.process.returncode is not None or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        assert "PTY_READY" in managed.peek(65_536)

        await managed.write("round-trip\r")
        while "PTY_ECHO:round-trip" not in managed.peek(65_536):
            if managed.process.returncode is not None or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        assert "PTY_ECHO:round-trip" in managed.peek(65_536)
        assert await asyncio.wait_for(managed.process.wait(), timeout=30) == 0
        if managed._task is not None:
            await asyncio.wait_for(managed._task, timeout=5)
    finally:
        await managed.close()

    assert process_range.stopped is True
