"""Retained program environments for programmatic tool calling.

A deployment that selects the ``persistent`` execution mode keeps one program
environment per conversation and language.  A later program sees the variables
and functions an earlier program created, so the model can build a result over
several turns instead of recomputing it each time.

A retained environment runs in the same operating-system cage as a one-shot
program.  It is created lazily, reused only by the same tenant, user,
conversation, language, workspace, and sandbox mode, and cleared on timeout,
cancellation, process failure, conversation deletion, or idle expiry.  The
environment is never durable: a restart starts it again from an empty state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.deepspace.services.ptc_runtime import (
    MAX_ERROR_CHARS,
    MAX_OUTPUT_CHARS,
    ProgramOutcome,
    ProgramRuntimeError,
    ProgramToolCall,
    _interpreter,
    _interpreter_root,
)
from app.deepspace.services.sandbox_backend import BwrapBackend, build_sandbox_command
from app.deepspace.services.subprocess_stdio import SubprocessStdio, spawn_with_stdio
from app.deepspace.services.tools._base import ToolContext
from app.deepspace.services.workspace_root import workspace_root_for

logger = logging.getLogger(__name__)

#: The line prefix that separates bridge control frames from program output.
FRAME_MARKER = "__PTC_FRAME__"

#: The largest program one retained evaluation may carry, in characters.
MAX_PROGRAM_CHARS = 100_000

#: The bridge that serves retained Python evaluations.
PERSISTENT_PYTHON_BRIDGE = r'''
"""Retained Python environment for DeepSpace programmatic tool calling."""

import ast
import asyncio
import io
import json
import sys
import traceback

FRAME = "__PTC_FRAME__"


class Channel:
    """The request/response channel to the DeepSpace host."""

    def __init__(self):
        self._next_id = 0

    def send(self, payload):
        """Send one control frame to the host."""
        sys.__stdout__.write(FRAME + json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.__stdout__.flush()

    def call(self, name, arguments=None, **keywords):
        """Run one host tool and return its result."""
        merged = dict(arguments or {})
        merged.update(keywords)
        self._next_id += 1
        call_id = self._next_id
        self.send({"t": "call", "id": call_id, "name": str(name), "arguments": merged})
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("The DeepSpace host closed the tool channel.")
            text = line.strip()
            if text.startswith(FRAME):
                text = text[len(FRAME):]
            if not text:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if frame.get("t") != "response":
                continue
            if int(frame.get("id") or 0) != call_id:
                continue
            if frame.get("ok"):
                return frame.get("value")
            raise RuntimeError(str(frame.get("error") or "The tool call failed."))


class Output(io.TextIOBase):
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

    @property
    def encoding(self):
        """Report the output encoding."""
        return "utf-8"


def contains(nodes, kind):
    """Report whether a top-level statement contains a node kind."""
    for node in nodes:
        if isinstance(node, kind):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if contains(ast.iter_child_nodes(node), kind):
            return True
    return False


def run_cell(source, namespace):
    """Evaluate one cell in a namespace that survives between calls.

    A program with no top-level ``await`` or ``return`` keeps its top-level
    declarations in the namespace.  A program with either is wrapped in one
    function, so its declarations are local to that call; this matches the
    documented retained-state rules.
    """
    module = ast.parse(source, "<cell>", "exec")
    has_await = contains(module.body, ast.Await)
    has_return = contains(module.body, ast.Return)
    if has_await or has_return:
        body = "\n".join(
            ("    " + line) if line.strip() else line for line in source.splitlines()
        )
        if not body.strip():
            body = "    pass"
        name = "__deepspace_cell__"
        if has_await:
            wrapper = "async def %s():\n%s\n" % (name, body)
            exec(compile(wrapper, "<cell>", "exec"), namespace)
            return asyncio.run(namespace.pop(name)())
        wrapper = "def %s():\n%s\n" % (name, body)
        exec(compile(wrapper, "<cell>", "exec"), namespace)
        return namespace.pop(name)()
    if module.body and isinstance(module.body[-1], ast.Expr):
        head = ast.Module(body=module.body[:-1], type_ignores=[])
        exec(compile(head, "<cell>", "exec"), namespace)
        return eval(
            compile(ast.Expression(module.body[-1].value), "<cell>", "eval"),
            namespace,
        )
    exec(compile(module, "<cell>", "exec"), namespace)
    return None


def main():
    """Serve evaluations until the host closes the channel."""
    channel = Channel()
    namespace = {
        "__name__": "__deepspace_program__",
        "tool": channel.call,
        "call_tool": channel.call,
    }
    sys.stdout = Output(channel)
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        text = line.strip()
        if text.startswith(FRAME):
            text = text[len(FRAME):]
        try:
            frame = json.loads(text)
        except ValueError:
            continue
        kind = frame.get("t")
        if kind == "close":
            return 0
        if kind != "eval":
            continue
        eval_id = int(frame.get("id") or 0)
        program = str(frame.get("program") or "")
        try:
            value = run_cell(program, namespace)
        except SystemExit:
            raise
        except BaseException as exc:
            channel.send(
                {
                    "t": "failed",
                    "id": eval_id,
                    "kind": "exception",
                    "message": "".join(
                        traceback.format_exception_only(type(exc), exc)
                    ).strip()[:20000],
                }
            )
        else:
            channel.send({"t": "result", "id": eval_id, "value": value})


if __name__ == "__main__":
    raise SystemExit(main())
'''


#: The bootstrap that serves retained Node.js evaluations.
PERSISTENT_NODE_BOOTSTRAP = r"""
'use strict';

const readline = require('readline');
const util = require('util');
const vm = require('vm');
const path = require('path');
const { createRequire } = require('module');

const FRAME = '__PTC_FRAME__';
const workspace = process.env.DEEPSPACE_PTC_WORKSPACE || process.cwd();
const input = readline.createInterface({ input: process.stdin });
const pending = new Map();
let sequence = 0;

function send(frame) {
  process.stdout.write(FRAME + JSON.stringify(frame) + '\n');
}

function tool(name, args = {}) {
  const id = `call_${++sequence}`;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ t: 'call', id, name, arguments: args });
  });
}

function writeOutput(text) {
  send({ t: 'out', text: String(text) });
}

const sandboxConsole = {
  log: (...args) => writeOutput(util.format(...args) + '\n'),
  info: (...args) => writeOutput(util.format(...args) + '\n'),
  warn: (...args) => writeOutput(util.format(...args) + '\n'),
  error: (...args) => writeOutput(util.format(...args) + '\n'),
  debug: (...args) => writeOutput(util.format(...args) + '\n'),
};

const sandbox = vm.createContext({
  tool,
  console: sandboxConsole,
  require: createRequire(path.join(workspace, 'program.js')),
  process: {
    env: {},
    argv: [],
    cwd: () => workspace,
    platform: process.platform,
  },
  Buffer,
  URL,
  URLSearchParams,
  TextEncoder,
  TextDecoder,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  queueMicrotask,
  structuredClone,
});
sandbox.global = sandbox;
sandbox.globalThis = sandbox;

input.on('line', async (line) => {
  let frame;
  try {
    const text = line.startsWith(FRAME) ? line.slice(FRAME.length) : line;
    frame = JSON.parse(text);
  } catch (error) {
    return;
  }
  if (frame.t === 'response') {
    const call = pending.get(frame.id);
    if (!call) return;
    pending.delete(frame.id);
    if (frame.ok) call.resolve(frame.result);
    else call.reject(new Error(frame.error || 'The tool call failed.'));
    return;
  }
  if (frame.t === 'close') {
    process.exit(0);
  }
  if (frame.t !== 'eval') return;
  const id = frame.id;
  const source = String(frame.program || '');
  try {
    let value;
    try {
      const script = new vm.Script(source, { filename: 'program.js' });
      value = script.runInContext(sandbox);
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      const wrapper = new vm.Script(
        '(async () => {\n' + source + '\n})()',
        { filename: 'program.js' },
      );
      value = wrapper.runInContext(sandbox);
    }
    if (value && typeof value.then === 'function') value = await value;
    send({ t: 'result', id, value: value === undefined ? null : value });
  } catch (error) {
    send({
      t: 'failed',
      id,
      kind: 'exception',
      message: String(error && error.stack ? error.stack : error).slice(0, 20000),
    });
  }
});

input.on('close', () => process.exit(0));
"""


@dataclass(slots=True)
class _Execution:
    """One evaluation in flight and the output it produced."""

    eval_id: int
    future: asyncio.Future[None]
    output: list[str] = field(default_factory=list)
    tool_calls: list[ProgramToolCall] = field(default_factory=list)
    value: Any = None
    status: str = "ok"
    error: str | None = None


#: A host tool binding: receives the program's arguments and returns a value.
ToolBinding = Callable[[dict[str, Any]], Awaitable[Any]]


class RetainedSessionError(RuntimeError):
    """A retained environment could not be created or used."""


class RetainedProgramSession:
    """One sandboxed program environment retained for a conversation."""

    def __init__(
        self,
        *,
        context: ToolContext,
        language: str,
        workspace_root: Path,
        session_id: str,
        grace_ms: int = 3_000,
    ) -> None:
        """Create a session without starting its process.

        Args:
            context: The tool context that owns the environment.
            language: ``python`` or ``javascript``.
            workspace_root: The workspace the environment may reach.
            session_id: A stable identifier used in the temporary directory.
            grace_ms: How long a clean shutdown may take before a kill.
        """
        self.context = context
        self.language = language
        self.workspace_root = workspace_root
        self.session_id = session_id
        self.grace_ms = grace_ms
        self.closed = False
        self.loop = asyncio.get_running_loop()
        self.last_used = time.monotonic()
        self.backend = "none"
        self._process_handle: Any = None
        self._process: asyncio.subprocess.Process | None = None
        self._directory: Path | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._execution: _Execution | None = None
        self._bindings: dict[str, ToolBinding] = {}
        self._max_tool_calls = 0
        self._on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
        self._event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    async def start(self) -> None:
        """Create the cage and start the retained child process."""
        self._directory = Path(tempfile.mkdtemp(prefix=f"deepspace-ptc-{self.session_id[:8]}-"))
        try:
            if self.language == "javascript":
                await self._start_node()
            else:
                await self._start_python()
        except BaseException:
            await self.close()
            raise
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _start_python(self) -> None:
        """Start the retained Python bridge."""
        assert self._directory is not None
        bridge = self._directory / "bridge.py"
        bridge.write_text(PERSISTENT_PYTHON_BRIDGE, encoding="utf-8")
        interpreter = _interpreter()
        native_argv = [interpreter, str(bridge)]
        sandbox = build_sandbox_command(
            self.context.settings,
            native_argv,
            workspace=self.workspace_root,
            cwd=self._directory,
            read_only=True,
            network=False,
            extra_read_only_paths=(_interpreter_root(interpreter), str(self._directory)),
        )
        if not sandbox.sandboxed or (
            self.context.sandbox_mode == "workspace-write"
            and sandbox.backend not in {"bwrap", "sandbox-exec"}
        ):
            raise ProgramRuntimeError(
                "A retained program needs an operating-system sandbox. Install bubblewrap "
                "(Linux) or run on macOS, then try again."
            )
        self.backend = sandbox.backend
        self._process_handle = await spawn_with_stdio(
            list(sandbox.argv),
            cwd=str(self._directory),
            parent_environment={
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.workspace_root),
                "TMPDIR": "/tmp",  # nosec B108 - bubblewrap mounts a private tmpfs.
                "LANG": "C.UTF-8",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
            grace_ms=self.grace_ms,
        )
        self._process = self._process_handle.process

    async def _start_node(self) -> None:
        """Start the retained Node bootstrap."""
        assert self._directory is not None
        bootstrap = self._directory / "bootstrap.js"
        bootstrap.write_text(PERSISTENT_NODE_BOOTSTRAP, encoding="utf-8")
        executable = str(getattr(self.context.settings, "deepspace_ptc_node_executable", "node"))
        node_binary = shutil.which(executable)
        if node_binary is None:
            raise ProgramRuntimeError(f"Node.js executable {executable!r} was not found.")
        resolved = Path(node_binary).resolve()
        sandbox = BwrapBackend()
        if not sandbox.available():
            raise ProgramRuntimeError(
                "The retained JavaScript runtime needs bubblewrap. Install bubblewrap "
                "(bwrap) and try again."
            )
        node_root = resolved.parent.parent
        command = sandbox.wrap(
            [str(resolved), str(bootstrap)],
            workspace=self.workspace_root,
            cwd=self.workspace_root,
            network=False,
            read_only=True,
            extra_read_only_paths=(
                str(self._directory),
                str(node_root),
                str(node_root.parent),
            ),
        )
        self.backend = command.backend
        self._process_handle = await spawn_with_stdio(
            list(command.argv),
            cwd=str(self.workspace_root),
            parent_environment={
                "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(self.workspace_root),
                "TMPDIR": "/tmp",
                "NODE_OPTIONS": (
                    "--max-old-space-size="
                    f"{int(getattr(self.context.settings, 'deepspace_ptc_node_max_old_generation_mb', 512))}"
                ),
                "DEEPSPACE_PTC_WORKSPACE": str(self.workspace_root),
            },
            stdio=SubprocessStdio(stdin="pipe", stdout="pipe", stderr="pipe"),
            stream_limit=int(
                getattr(self.context.settings, "deepspace_ptc_node_max_message_bytes", 134_217_728)
            ),
            grace_ms=int(getattr(self.context.settings, "deepspace_ptc_node_grace_ms", 3_000)),
        )
        self._process = self._process_handle.process

    async def _read_stdout(self) -> None:
        """Read control frames and plain output until the child exits."""
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text.startswith(FRAME_MARKER):
                    try:
                        frame = json.loads(text[len(FRAME_MARKER) :])
                    except ValueError:
                        self._append_output(text + "\n")
                        continue
                    await self._handle_frame(frame)
                else:
                    self._append_output(text + "\n")
        finally:
            self._fail_pending("The retained program environment exited.")
            self.closed = True

    async def _read_stderr(self) -> None:
        """Drain stderr so a full pipe cannot block the child."""
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            chunk = await process.stderr.read(65_536)
            if not chunk:
                return

    def _append_output(self, text: str) -> None:
        execution = self._execution
        if execution is not None and len("".join(execution.output)) < MAX_OUTPUT_CHARS:
            execution.output.append(text)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """Answer one tool request or settle the current evaluation."""
        kind = str(frame.get("t") or "")
        if kind == "call":
            await self._answer_call(frame)
            return
        if kind == "out":
            self._append_output(str(frame.get("text") or ""))
            return
        if kind == "event":
            event_type = str(frame.get("type") or frame.get("name") or "")
            payload = frame.get("payload")
            if self._event_sink is not None and event_type:
                await self._event_sink(event_type, payload if isinstance(payload, dict) else {})
            return
        execution = self._execution
        if execution is None or int(frame.get("id") or 0) != execution.eval_id:
            return
        if kind == "result":
            execution.value = frame.get("value")
            execution.status = "ok"
        elif kind == "failed":
            execution.status = "failed"
            execution.error = str(frame.get("message") or "The program failed.")[:MAX_ERROR_CHARS]
        else:
            return
        if not execution.future.done():
            execution.future.set_result(None)

    async def _answer_call(self, frame: dict[str, Any]) -> None:
        """Run one host tool and answer the program."""
        execution = self._execution
        if execution is None:
            return
        name = str(frame.get("name") or "")
        raw_arguments = frame.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
        call = ProgramToolCall(name=name, arguments=arguments, status="refused")
        value: Any = None
        if len(execution.tool_calls) >= self._max_tool_calls:
            call.error = f"This program may call at most {self._max_tool_calls} host tools."
        elif name not in self._bindings:
            call.error = f"The tool {name!r} is not available to this program."
        else:
            try:
                value = await self._bindings[name](arguments)
            except Exception as exc:  # noqa: BLE001 - reported to the program
                call.status = "failed"
                call.error = str(exc)[:MAX_ERROR_CHARS] or exc.__class__.__name__
            else:
                call.status = "ok"
                call.error = None
        execution.tool_calls.append(call)
        await self._send(
            {
                "t": "response",
                "id": frame.get("id"),
                "ok": call.status == "ok",
                "result": value if call.status == "ok" else None,
                "error": call.error,
            }
        )

    async def _send(self, frame: dict[str, Any]) -> None:
        """Write one control frame to the child's standard input."""
        process = self._process
        if process is None or process.stdin is None or self.closed:
            return
        payload = FRAME_MARKER + json.dumps(frame, ensure_ascii=False, default=str) + "\n"
        async with self._write_lock:
            try:
                process.stdin.write(payload.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self.closed = True

    def _fail_pending(self, message: str) -> None:
        """Settle an in-flight evaluation after the child goes away."""
        execution = self._execution
        if execution is None or execution.future.done():
            return
        execution.status = "failed"
        execution.error = message
        execution.future.set_result(None)

    async def run(
        self,
        *,
        program: str,
        bindings: dict[str, ToolBinding],
        max_tool_calls: int,
        timeout_seconds: float,
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProgramOutcome:
        """Evaluate one program in the retained environment.

        Args:
            program: The program text.
            bindings: The host tools the program may call.
            max_tool_calls: The largest number of host tool calls.
            timeout_seconds: The wall-clock budget for this evaluation.
            on_event: An optional workflow event sink.

        Returns:
            The evaluation outcome.  A timeout or process failure closes the
            environment so the next call starts from an empty state.
        """
        if self.closed:
            raise RetainedSessionError("The retained program environment is closed.")
        if self._run_lock.locked():
            return ProgramOutcome(
                status="failed",
                error=(
                    "A program is already running in this conversation's retained "
                    "environment; wait for it to finish and try again."
                ),
                persistent=True,
            )
        async with self._run_lock:
            loop = asyncio.get_running_loop()
            execution = _Execution(eval_id=1, future=loop.create_future())
            self._execution = execution
            self._bindings = bindings
            self._max_tool_calls = max(0, int(max_tool_calls))
            self._event_sink = on_event
            started = time.monotonic()
            await self._send({"t": "eval", "id": execution.eval_id, "program": program})
            try:
                await asyncio.wait_for(execution.future, timeout=max(1.0, timeout_seconds))
            except TimeoutError:
                execution.status = "timeout"
                execution.error = (
                    f"The program did not finish within {int(timeout_seconds)} seconds. "
                    "The retained environment was cleared."
                )
                await self.close()
            self.last_used = time.monotonic()
            outcome = ProgramOutcome(
                status=execution.status,
                output="".join(execution.output)[:MAX_OUTPUT_CHARS],
                value=execution.value,
                error=execution.error,
                tool_calls=list(execution.tool_calls),
                duration_ms=int((time.monotonic() - started) * 1000),
                sandboxed=True,
                sandbox_backend=self.backend,
                persistent=True,
            )
            self._execution = None
            self._event_sink = None
            return outcome

    async def close(self) -> None:
        """Stop the child and remove its temporary files."""
        if self.closed and self._process is None:
            return
        process_handle = self._process_handle
        if process_handle is not None and not self.closed:
            # Give the bridge a chance to exit cleanly before the grace kill.
            with contextlib.suppress(Exception):
                await self._send({"t": "close"})
        self.closed = True
        if process_handle is not None:
            try:
                await asyncio.wait_for(process_handle.wait(), timeout=max(0, self.grace_ms) / 1000)
            except (TimeoutError, Exception):  # noqa: BLE001 - close is best effort
                with contextlib.suppress(Exception):
                    await process_handle.terminate_and_wait()
            self._process = None
            self._process_handle = None
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending("The retained program environment was closed.")
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None

    def kill(self) -> None:
        """Stop the child without waiting, for a stale event loop."""
        self.closed = True
        process = self._process
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None


class PtcSessionManager:
    """Own retained environments, one per conversation and language."""

    def __init__(self) -> None:
        """Create an empty manager."""
        self._sessions: dict[tuple[str, ...], RetainedProgramSession] = {}
        self._lock = asyncio.Lock()

    def _key(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        language: str,
        workspace_root: Path,
        sandbox_mode: str,
    ) -> tuple[str, ...]:
        """Return the ownership key of one retained environment."""
        return (
            tenant_id,
            user_id,
            conversation_id,
            language,
            str(workspace_root.resolve()),
            sandbox_mode,
        )

    async def run(
        self,
        *,
        context: ToolContext,
        language: str,
        program: str,
        bindings: dict[str, ToolBinding],
        max_tool_calls: int,
        timeout_seconds: float,
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> ProgramOutcome:
        """Run one program in the conversation's retained environment.

        Args:
            context: The tool context that owns the environment.
            language: ``python`` or ``javascript``.
            program: The program text.
            bindings: The host tools the program may call.
            max_tool_calls: The largest number of host tool calls.
            timeout_seconds: The wall-clock budget for this evaluation.
            on_event: An optional workflow event sink.

        Returns:
            The evaluation outcome.
        """
        await self._evict_idle()
        tenant_id = str(context.auth.tenant_id)
        user_id = str(context.auth.user_id)
        workspace_root = context.workspace_root or workspace_root_for(
            context.settings, context.auth.tenant_id, context.auth.user_id
        )
        sandbox_mode = str(context.sandbox_mode or "workspace-write")
        key = self._key(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=str(context.conversation_id),
            language=language,
            workspace_root=workspace_root,
            sandbox_mode=sandbox_mode,
        )
        session: RetainedProgramSession
        async with self._lock:
            current = self._sessions.get(key)
            if current is not None and (
                current.closed or current.loop is not asyncio.get_running_loop()
            ):
                self._sessions.pop(key, None)
                await self._dispose(current)
                current = None
            if current is None:
                await self._make_room(context.settings)
                current = RetainedProgramSession(
                    context=context,
                    language=language,
                    workspace_root=workspace_root,
                    session_id=uuid.uuid4().hex,
                )
                await current.start()
                self._sessions[key] = current
            session = current
            # The environment is conversation-owned, but every run belongs to
            # the current request, so the current context supplies settings,
            # cancellation, and the sandbox policy.
            session.context = context
        outcome = await session.run(
            program=program,
            bindings=bindings,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
            on_event=on_event,
        )
        if session.closed:
            async with self._lock:
                if self._sessions.get(key) is session:
                    self._sessions.pop(key, None)
        return outcome

    async def _dispose(self, session: RetainedProgramSession) -> None:
        """Close one environment, or kill it from a stale event loop.

        Args:
            session: The environment to dispose.
        """
        if session.loop is asyncio.get_running_loop():
            await session.close()
        else:
            session.kill()

    async def _make_room(self, settings: Any) -> None:
        """Close the least-recently-used session when the cap is reached.

        Args:
            settings: The deployment settings that carry the session cap.
        """
        maximum = int(getattr(settings, "deepspace_ptc_persistent_max_sessions", 32) or 32)
        if len(self._sessions) < max(1, maximum):
            return
        oldest_key = min(self._sessions, key=lambda item: self._sessions[item].last_used)
        oldest = self._sessions.pop(oldest_key)
        await self._dispose(oldest)

    async def _evict_idle(self) -> None:
        """Close sessions that have been idle past their configured budget."""
        now = time.monotonic()
        expired: list[RetainedProgramSession] = []
        async with self._lock:
            for key, session in list(self._sessions.items()):
                idle_seconds = int(
                    getattr(
                        session.context.settings,
                        "deepspace_ptc_persistent_idle_seconds",
                        900,
                    )
                    or 900
                )
                if now - session.last_used > idle_seconds:
                    expired.append(self._sessions.pop(key))
        for session in expired:
            await self._dispose(session)

    async def release(
        self,
        *,
        tenant_id: Any = None,
        user_id: Any = None,
        conversation_id: Any = None,
        language: str | None = None,
    ) -> int:
        """Release matching retained environments.

        Args:
            tenant_id: The owning tenant, or None for every tenant.
            user_id: The owning user, or None for every user.
            conversation_id: The conversation, or None for every conversation.
            language: The language, or None for every language.

        Returns:
            The number of environments released.
        """
        tenant = str(tenant_id) if tenant_id is not None else None
        user = str(user_id) if user_id is not None else None
        conversation = str(conversation_id) if conversation_id is not None else None
        released: list[RetainedProgramSession] = []
        async with self._lock:
            for key, _session in list(self._sessions.items()):
                if tenant is not None and key[0] != tenant:
                    continue
                if user is not None and key[1] != user:
                    continue
                if conversation is not None and key[2] != conversation:
                    continue
                if language is not None and key[3] != language:
                    continue
                released.append(self._sessions.pop(key))
        for session in released:
            await self._dispose(session)
        return len(released)

    async def shutdown(self) -> None:
        """Release every retained environment."""
        await self.release()


#: The process-wide manager.  It owns no durable state and is emptied on
#: shutdown, idle expiry, cancellation, and conversation deletion.
_MANAGER = PtcSessionManager()


def ptc_session_manager() -> PtcSessionManager:
    """Return the process-wide retained-environment manager."""
    return _MANAGER


async def run_retained_program(
    *,
    context: ToolContext,
    language: str,
    program: str,
    tool_names: list[str],
    timeout_seconds: float,
    max_tool_calls: int,
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> ProgramOutcome:
    """Run one program in a retained environment.

    Args:
        context: The tool context that owns the environment.
        language: ``python`` or ``javascript``.
        program: The program text.
        tool_names: The tools the program may call.
        timeout_seconds: The wall-clock budget.
        max_tool_calls: The largest number of host tool calls.
        on_event: An optional workflow event sink.

    Returns:
        The evaluation outcome.

    Raises:
        ProgramRuntimeError: When the deployment disabled the language or the
            conversation has no tool executor.
    """
    if not isinstance(program, str) or not program.strip():
        raise ProgramRuntimeError("A program is required.")
    if len(program) > MAX_PROGRAM_CHARS:
        raise ProgramRuntimeError("The program exceeds the 100,000 character safety limit.")
    executor = context.native_executor
    if executor is None:
        raise ProgramRuntimeError("This conversation has no tool executor.")

    def binding(tool_name: str) -> ToolBinding:
        """Bind one allowed tool name to the conversation executor."""

        async def call(arguments: dict[str, Any]) -> Any:
            return await executor(tool_name, arguments)

        return call

    from app.deepspace.services.ptc_runtime import MAX_PROGRAM_TOOLS

    bindings = {str(name): binding(str(name)) for name in tool_names[:MAX_PROGRAM_TOOLS]}
    outcome = await _MANAGER.run(
        context=context,
        language=language,
        program=program,
        bindings=bindings,
        max_tool_calls=max_tool_calls,
        timeout_seconds=timeout_seconds,
        on_event=on_event,
    )
    if context.cancel_check is not None and context.cancel_check() and outcome.status == "ok":
        outcome.status = "cancelled"
        outcome.error = "The program was cancelled."
        await _MANAGER.release(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            language=language,
        )
    return outcome


async def release_ptc_sessions(
    *,
    tenant_id: Any = None,
    user_id: Any = None,
    conversation_id: Any = None,
    language: str | None = None,
) -> int:
    """Release matching retained environments."""
    return await _MANAGER.release(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        language=language,
    )


async def shutdown_ptc_sessions() -> None:
    """Release every retained environment during orderly shutdown."""
    await _MANAGER.shutdown()


__all__ = [
    "FRAME_MARKER",
    "MAX_PROGRAM_CHARS",
    "PERSISTENT_NODE_BOOTSTRAP",
    "PERSISTENT_PYTHON_BRIDGE",
    "PtcSessionManager",
    "RetainedProgramSession",
    "RetainedSessionError",
    "ptc_session_manager",
    "release_ptc_sessions",
    "run_retained_program",
    "shutdown_ptc_sessions",
]
