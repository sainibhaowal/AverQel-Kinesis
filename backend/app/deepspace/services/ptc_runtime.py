"""Programmatic tool calling for DeepSpace.

A model can write one program that calls host tools as ordinary function
calls, and the program returns only what it printed and what it returned.
That replaces a long chain of provider round trips with a single decision
and a single result.

The program runs in a child process, not in the API or worker process, so a
failing program cannot take the turn down with it.  The child reaches the
host over a request/response channel on its standard streams: the program
writes a request frame, the host runs the requested tool through the same
policy and tenant scope as any other call, and the host writes the result
back.  The bridge forwards the program's own prints as frames on the same
channel, so the program's output and its tool requests keep their order and
neither can corrupt the other.

The child is confined by the operating-system cage the deployment already
uses for the shell tools.  Programmatic tool calling has no cage fallback:
without one, the program would run with the worker's own privileges, so the
runtime refuses the call instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.deepspace.services.sandbox_backend import (
    BwrapBackend,
    SandboxExecBackend,
    build_sandbox_command,
)
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workspace_root import workspace_root_for

logger = logging.getLogger(__name__)

#: The directory under the workspace root that holds one program's files.
PTC_DIRECTORY = ".deepspace/ptc"

#: The largest program one call may carry, in characters.
MAX_PROGRAM_CHARS = 100_000

#: The largest number of characters one program's output may carry.
MAX_OUTPUT_CHARS = 200_000

#: The largest number of characters one program's error output may carry.
MAX_ERROR_CHARS = 20_000

#: The largest number of tools one program may name.
MAX_PROGRAM_TOOLS = 16

#: The bridge program that runs the model's program and speaks the protocol.
#:
#: It is written into the program's own directory for the duration of the
#: run, so the child needs nothing from the API container's file system
#: except the interpreter itself.
BRIDGE_SOURCE = r'''
"""The DeepSpace programmatic-tool-calling bridge.

The bridge runs one model-written program and gives it a ``tool`` function
that reaches the host over this process's standard streams.  The host runs
the requested tool and sends the result back, so the program never touches
the host's own modules.
"""

import json
import sys


class ToolResult(dict):
    """One tool result. It may also be awaited inside an async program."""

    def __await__(self):
        async def _value():
            return self

        return _value().__await__()


class Channel:
    """The request/response channel to the DeepSpace host."""

    def __init__(self, writer):
        self._writer = writer
        self._next_id = 0

    def send(self, payload):
        """Send one frame to the host."""
        self._writer.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        self._writer.flush()

    def call(self, name, arguments=None, **keywords):
        """Run one host tool and return its result.

        Args:
            name: The tool name.
            arguments: The tool arguments as a mapping.
            keywords: The tool arguments as keywords.

        Returns:
            The tool result. The value may be awaited.
        """
        merged = dict(arguments or {})
        merged.update(keywords)
        self._next_id += 1
        call_id = self._next_id
        self.send(
            {"t": "call", "id": call_id, "name": str(name), "arguments": merged}
        )
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("The DeepSpace host closed the tool channel.")
        frame = json.loads(line)
        if frame.get("t") == "error":
            raise RuntimeError(str(frame.get("message") or "The tool call failed."))
        return ToolResult(frame.get("value") or {})


class Output:
    """Forward the program's prints to the host as output frames."""

    def __init__(self, channel):
        self._channel = channel

    def write(self, text):
        """Send one piece of program output to the host."""
        if text:
            self._channel.send({"t": "out", "text": text})
        return len(text)

    def flush(self):
        """Keep the program's ``print`` contract without a real stream."""
        return None

    def isatty(self):
        """Report that the program's output is not a terminal."""
        return False

    @property
    def encoding(self):
        """Report the output encoding."""
        return "utf-8"


def compile_program(source, path):
    """Compile the program, accepting a top-level ``return``.

    Args:
        source: The program text.
        path: The name the program is compiled under.

    Returns:
        The compiled code and whether it must be called as a function.
    """
    try:
        return compile(source, path, "exec"), False
    except SyntaxError as exc:
        if "return" not in str(exc.msg).lower():
            raise
        body = "\n".join(
            ("    " + line) if line.strip() else line for line in source.splitlines()
        )
        wrapped = "def __deepspace_main__():\n" + body + "\n"
        return compile(wrapped, path, "exec"), True


def main():
    """Run the program and report its result to the host."""
    program_path = sys.argv[1]
    channel = Channel(sys.stdout)
    with open(program_path, "r", encoding="utf-8") as handle:
        source = handle.read()
    try:
        compiled, wrapped = compile_program(source, "program.py")
    except SyntaxError as exc:
        channel.send(
            {
                "t": "failed",
                "message": "The program has a syntax error: %s (line %s)" % (exc.msg, exc.lineno),
            }
        )
        return 2
    namespace = {
        "__name__": "__deepspace_program__",
        "tool": channel.call,
        "call_tool": channel.call,
    }
    saved_stdout = sys.stdout
    sys.stdout = Output(channel)
    try:
        exec(compiled, namespace)
        value = namespace.get("result")
        if wrapped:
            value = namespace["__deepspace_main__"]()
    except BaseException as exc:  # noqa: BLE001
        channel.send(
            {"t": "failed", "message": "%s: %s" % (type(exc).__name__, exc)}
        )
        return 1
    finally:
        sys.stdout = saved_stdout
    channel.send({"t": "result", "value": value})
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


@dataclass(slots=True)
class ProgramToolCall:
    """One tool the program asked the host to run."""

    name: str
    arguments: dict[str, Any]
    status: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable record of this call."""
        return {
            "name": self.name,
            "arguments": self.arguments,
            "status": self.status,
            "error": self.error,
        }


@dataclass(slots=True)
class ProgramOutcome:
    """The result of one program run.

    Attributes:
        status: ``ok``, ``failed``, ``timeout``, ``cancelled``, or
            ``unavailable``.
        output: What the program printed.
        value: What the program returned.
        error: The failure detail, when the program did not complete.
        tool_calls: The tools the program asked the host to run.
        duration_ms: The wall-clock time the run used.
        sandboxed: Whether an operating-system cage confined the program.
        sandbox_backend: The cage that confined the program.
    """

    status: str
    output: str = ""
    value: Any = None
    error: str | None = None
    tool_calls: list[ProgramToolCall] = field(default_factory=list)
    duration_ms: int = 0
    sandboxed: bool = False
    sandbox_backend: str = "none"

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable result the model receives."""
        return {
            "status": self.status,
            "output": self.output,
            "value": self.value,
            "error": self.error,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "tool_call_count": len(self.tool_calls),
            "duration_ms": self.duration_ms,
            "sandboxed": self.sandboxed,
            "sandbox_backend": self.sandbox_backend,
        }


class ProgramRuntimeError(RuntimeError):
    """A program could not be prepared or run."""


def local_cage_available() -> bool:
    """Report whether this host can confine a child process."""
    return BwrapBackend().available() or SandboxExecBackend().available()


def _interpreter() -> str:
    """Return the interpreter that runs the bridge.

    The program runs inside the operating-system cage, which exposes the
    system directories rather than the worker's virtual environment.  A
    system interpreter is therefore the one that survives the cage.

    Raises:
        ProgramRuntimeError: When no system interpreter is present.
    """
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("python3")
    if found:
        return found
    raise ProgramRuntimeError(
        "This host has no system Python interpreter, so a program cannot run inside the sandbox."
    )


def _interpreter_root(interpreter: str) -> str:
    """Return the system directory that holds the interpreter."""
    resolved = os.path.realpath(interpreter)
    for root in ("/usr", "/usr/local", "/bin", "/opt"):
        if resolved.startswith(f"{root}/"):
            return root
    return "/usr"


def _program_directory(root: Path, run_id: str) -> Path:
    """Create and return the directory that holds one program's files."""
    directory = root / PTC_DIRECTORY / run_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_program_files(directory: Path, program: str) -> tuple[Path, Path]:
    """Write the bridge and the model program, and return their paths."""
    bridge_path = directory / "bridge.py"
    program_path = directory / "program.py"
    bridge_path.write_text(BRIDGE_SOURCE, encoding="utf-8")
    program_path.write_text(program, encoding="utf-8")
    return bridge_path, program_path


async def _run_program(
    *,
    context: ToolContext,
    program: str,
    allowed_tools: dict[str, Any],
    directory: Path,
    timeout_seconds: int,
    max_tool_calls: int,
) -> ProgramOutcome:
    """Run one program and service the tool calls it makes.

    Args:
        context: The calling request's tool context.
        program: The model-written program.
        allowed_tools: The tools the program may call, by name.
        directory: The directory that holds the program's files.
        timeout_seconds: The wall-clock budget for the program.
        max_tool_calls: The largest number of host tool calls the program may
            make.

    Returns:
        The outcome of the run.
    """
    bridge_path, program_path = _write_program_files(directory, program)
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    interpreter = _interpreter()
    native_argv = [interpreter, str(bridge_path), str(program_path)]
    sandbox = build_sandbox_command(
        context.settings,
        native_argv,
        workspace=root,
        cwd=directory,
        read_only=False,
        network=False,
        extra_read_only_paths=(_interpreter_root(interpreter),),
    )
    if not sandbox.sandboxed:
        raise ProgramRuntimeError(
            "Programmatic tool calling needs an operating-system sandbox. Install bubblewrap "
            "(Linux) or run on macOS, then try again."
        )
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *sandbox.argv,
        cwd=str(directory),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(root),
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    outcome = ProgramOutcome(status="ok", sandboxed=True, sandbox_backend=sandbox.backend)
    output_parts: list[str] = []
    error_parts: list[str] = []

    async def _answer(call_id: int, name: str, arguments: dict[str, Any]) -> None:
        """Run one requested tool and send its result back to the program."""
        call = ProgramToolCall(name=name, arguments=arguments, status="refused")
        outcome.tool_calls.append(call)
        if name not in allowed_tools:
            call.error = f"The tool '{name}' is not available to this program."
        elif len(outcome.tool_calls) > max_tool_calls:
            call.error = f"This program may call at most {max_tool_calls} host tools."
        else:
            executor = context.native_executor
            if executor is None:
                call.error = "This conversation has no tool executor."
            else:
                try:
                    value = await executor(name, arguments)
                except Exception as exc:  # noqa: BLE001
                    call.status = "failed"
                    call.error = str(exc)[:1_000]
                else:
                    call.status = "ok"
        if call.status == "ok":
            frame: dict[str, Any] = {"t": "result", "id": call_id, "value": value}
        else:
            frame = {"t": "error", "id": call_id, "message": call.error or "The tool call failed."}
        if process.stdin is None:
            return
        try:
            process.stdin.write(
                json.dumps(frame, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
            )
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return

    async def _drain(stream: asyncio.StreamReader | None, sink: list[str]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                return
            sink.append(chunk.decode("utf-8", errors="replace"))

    async def _serve() -> None:
        """Read the program's frames and answer its tool requests."""
        if process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                output_parts.append(text + "\n")
                continue
            kind = str(frame.get("t") or "")
            if kind == "call":
                raw_arguments = frame.get("arguments")
                await _answer(
                    int(frame.get("id") or 0),
                    str(frame.get("name") or ""),
                    raw_arguments if isinstance(raw_arguments, dict) else {},
                )
                continue
            if kind == "out":
                output_parts.append(str(frame.get("text") or ""))
                continue
            if kind == "result":
                outcome.value = frame.get("value")
                continue
            if kind == "failed":
                outcome.status = "failed"
                outcome.error = str(frame.get("message") or "The program failed.")

    server = asyncio.create_task(_serve())
    errors = asyncio.create_task(_drain(process.stderr, error_parts))
    try:
        await asyncio.wait_for(asyncio.gather(server, errors), timeout=timeout_seconds)
    except TimeoutError:
        outcome.status = "timeout"
        outcome.error = f"The program did not finish within {timeout_seconds} seconds."
    finally:
        for task in (server, errors):
            if not task.done():
                task.cancel()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            pass
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    outcome.output = "".join(output_parts)[:MAX_OUTPUT_CHARS]
    if not outcome.error and error_parts:
        stderr_text = "".join(error_parts)[:MAX_ERROR_CHARS].strip()
        if stderr_text:
            outcome.error = stderr_text
    if context.cancel_check is not None and context.cancel_check() and outcome.status == "ok":
        outcome.status = "cancelled"
        outcome.error = "The program was cancelled."
    return outcome


async def run_program(
    *,
    context: ToolContext,
    program: str,
    tool_names: list[str],
    timeout_seconds: int | None = None,
    max_tool_calls: int | None = None,
    language: str = "python",
) -> ProgramOutcome:
    """Run one model-written program that calls host tools.

    Args:
        context: The calling request's tool context.
        program: The model-written program.
        tool_names: The tools the program may call.
        timeout_seconds: The wall-clock budget, taken from the settings when
            omitted.
        max_tool_calls: The largest number of host tool calls, taken from the
            settings when omitted.
        language: The programming language ("python" or "javascript").

    Returns:
        The outcome of the run.

    Raises:
        ProgramRuntimeError: When the program cannot be prepared or confined.
    """
    if not bool(getattr(context.settings, "deepspace_ptc_enabled", True)):
        raise ProgramRuntimeError("Programmatic tool calling is disabled on this deployment.")
    if not isinstance(program, str) or not program.strip():
        raise ProgramRuntimeError("A program is required.")
    if len(program) > MAX_PROGRAM_CHARS:
        raise ProgramRuntimeError("The program exceeds the 100,000 character safety limit.")

    # Dispatch to appropriate runtime based on language
    if language == "javascript":
        # Use Node.js runtime
        if not getattr(context.settings, "deepspace_ptc_node_enabled", False):
            raise ProgramRuntimeError("JavaScript PTC runtime is disabled on this deployment.")
        if not local_cage_available():
            raise ProgramRuntimeError(
                "Programmatic tool calling needs an operating-system sandbox. Install bubblewrap "
                "(Linux) or run on macOS, then try again."
            )

        executor = context.native_executor
        if executor is None:
            raise ProgramRuntimeError("This conversation has no tool executor.")
        root = context.workspace_root or workspace_root_for(
            context.settings, context.auth.tenant_id, context.auth.user_id
        )

        def binding(tool_name: str) -> Any:
            """Bind one allowed tool name to the conversation executor."""

            async def call(arguments: dict[str, Any]) -> Any:
                return await executor(tool_name, arguments)

            return call

        bindings = {str(name): binding(str(name)) for name in tool_names[:MAX_PROGRAM_TOOLS]}
        budget_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else getattr(context.settings, "deepspace_ptc_timeout_seconds", 60)
        )
        call_budget = int(
            max_tool_calls
            if max_tool_calls is not None
            else getattr(context.settings, "deepspace_ptc_max_tool_calls", 24)
        )
        from app.deepspace.services.ptc_runtime_node import create_node_ptc_runtime

        node_runtime = create_node_ptc_runtime(context.settings)
        spec = node_runtime.resolve(
            program,
            bindings,
            str(root),
            budget_seconds * 1000,
            context.tool_policy.sandbox_policy if context.tool_policy else None,
            max_tool_calls=call_budget,
        )
        return await node_runtime.run(spec)

    # Default: Python runtime (existing logic)
    if not local_cage_available():
        raise ProgramRuntimeError(
            "Programmatic tool calling needs an operating-system sandbox. Install bubblewrap "
            "(Linux) or run on macOS, then try again."
        )
    root = context.workspace_root or workspace_root_for(
        context.settings, context.auth.tenant_id, context.auth.user_id
    )
    directory = _program_directory(root, uuid.uuid4().hex)
    allowed: dict[str, Any] = {str(name): None for name in tool_names[:MAX_PROGRAM_TOOLS]}
    try:
        return await _run_program(
            context=context,
            program=program,
            allowed_tools=allowed,
            directory=directory,
            timeout_seconds=int(
                timeout_seconds
                if timeout_seconds is not None
                else getattr(context.settings, "deepspace_ptc_timeout_seconds", 60)
            ),
            max_tool_calls=int(
                max_tool_calls
                if max_tool_calls is not None
                else getattr(context.settings, "deepspace_ptc_max_tool_calls", 24)
            ),
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "BRIDGE_SOURCE",
    "MAX_OUTPUT_CHARS",
    "MAX_PROGRAM_CHARS",
    "MAX_PROGRAM_TOOLS",
    "PTC_DIRECTORY",
    "ProgramOutcome",
    "ProgramRuntimeError",
    "ProgramToolCall",
    "local_cage_available",
    "run_program",
]
