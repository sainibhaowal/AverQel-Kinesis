"""Contained pywinpty host process and its framed terminal protocol.

DeepSpace starts this helper through ``spawn_with_stdio``. On Windows that
launcher creates it suspended, assigns it to a kill-on-close Job Object, and
only then resumes it. The PTY child inherits the helper's Job membership, so
neither the broker nor the shell can execute outside the assigned job during
startup.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
from typing import Any

_HEADER = struct.Struct("!BI")
_READY = 1
_OUTPUT = 2
_EXIT = 3
_ERROR = 4
_MAX_FRAME_BYTES = 65_536


def _write_frame(kind: int, payload: bytes, lock: threading.Lock) -> None:
    if len(payload) > _MAX_FRAME_BYTES:
        raise ValueError("Windows PTY frame exceeds the maximum size")
    output = sys.stdout.buffer
    with lock:
        output.write(_HEADER.pack(kind, len(payload)))
        output.write(payload)
        output.flush()


def _read_start_request() -> dict[str, Any]:
    line = sys.stdin.buffer.readline(_MAX_FRAME_BYTES + 1)
    if not line:
        raise ValueError("Windows PTY startup request is missing")
    if len(line) > _MAX_FRAME_BYTES:
        raise ValueError("Windows PTY startup request exceeds the maximum size")
    request = json.loads(line)
    if not isinstance(request, dict) or request.get("op") != "start":
        raise ValueError("Windows PTY startup request is invalid")
    argv = request.get("argv")
    env = request.get("env")
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
        raise ValueError("Windows PTY argv must be a non-empty string array")
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()
    ):
        raise ValueError("Windows PTY environment must contain only strings")
    for key in ("rows", "columns"):
        value = request.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
            raise ValueError(f"Windows PTY {key} must be between 1 and 500")
    cwd = request.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("Windows PTY working directory is required")
    return request


def _pump_output(pty: Any, writer_lock: threading.Lock, stopped: threading.Event) -> None:
    try:
        while not stopped.is_set():
            chunk = pty.fileobj.recv(_MAX_FRAME_BYTES)
            if not chunk:
                return
            _write_frame(_OUTPUT, chunk, writer_lock)
    except (OSError, EOFError, BrokenPipeError):
        return


def _handle_controls(pty: Any, writer_lock: threading.Lock, stopped: threading.Event) -> None:
    del writer_lock  # Output and control use separate directions.
    try:
        for line in sys.stdin.buffer:
            if len(line) > _MAX_FRAME_BYTES:
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    continue
                operation = request.get("op")
                if operation == "write" and isinstance(request.get("text"), str):
                    pty.write(request["text"])
                elif operation == "resize":
                    rows, columns = request.get("rows"), request.get("columns")
                    if (
                        isinstance(rows, int)
                        and not isinstance(rows, bool)
                        and isinstance(columns, int)
                        and not isinstance(columns, bool)
                        and 1 <= rows <= 500
                        and 1 <= columns <= 500
                    ):
                        pty.setwinsize(rows, columns)
                elif operation == "interrupt":
                    pty.sendintr()
                elif operation == "terminate":
                    pty.terminate(force=bool(request.get("force")))
            except (ValueError, TypeError, OSError, EOFError):
                continue
    finally:
        stopped.set()
        if pty.isalive():
            try:
                pty.terminate(force=True)
            except (OSError, EOFError):
                pass


def run_host() -> int:
    """Serve one real pywinpty session over the stdio frame protocol."""
    writer_lock = threading.Lock()
    stopped = threading.Event()
    try:
        request = _read_start_request()
        from winpty import PtyProcess

        pty = PtyProcess.spawn(
            request["argv"],
            cwd=request["cwd"],
            env=request["env"],
            dimensions=(request["rows"], request["columns"]),
        )
    except BaseException as exc:
        message = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=True).encode(
            "utf-8"
        )[:_MAX_FRAME_BYTES]
        try:
            _write_frame(_ERROR, message, writer_lock)
        except (OSError, BrokenPipeError):
            pass
        return 1

    _write_frame(_READY, struct.pack("!I", int(pty.pid)), writer_lock)
    output_thread = threading.Thread(
        target=_pump_output,
        args=(pty, writer_lock, stopped),
        name="deepspace-windows-pty-output",
        daemon=True,
    )
    control_thread = threading.Thread(
        target=_handle_controls,
        args=(pty, writer_lock, stopped),
        name="deepspace-windows-pty-control",
        daemon=True,
    )
    output_thread.start()
    control_thread.start()
    while pty.isalive() and not stopped.wait(0.05):
        pass

    try:
        status = pty.wait()
    except Exception:
        status = pty.exitstatus
    stopped.set()
    output_thread.join(timeout=1.0)
    try:
        pty.close(force=False)
    except Exception:
        pass
    exit_code = int(status) if isinstance(status, int) else 0
    exit_code = max(-127, min(exit_code, 127))
    _write_frame(_EXIT, struct.pack("!i", exit_code), writer_lock)
    return exit_code


if __name__ == "__main__":  # pragma: no cover - exercised on Windows hosts
    raise SystemExit(run_host())
