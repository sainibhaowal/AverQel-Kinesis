"""Node.js PTC runtime for DeepSpace.

Runs one model-written JavaScript program in a fresh Node process inside a
bubblewrap cage. The program calls host tools through a newline-delimited
channel on stdout/stdin:

* ``__PTC_REQUEST__{json}`` asks the host to run a tool.
* ``__PTC_RESPONSE__{json}`` carries the host's answer.
* ``__PTC_RESULT__{json}`` carries the program's return value.

Every other stdout line is program output. The protocol markers keep host
traffic separate from ``console.log`` without taking over the descriptors a
program may use itself.
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.deepspace.services.ptc_runtime import (
    ProgramOutcome,
    ProgramToolCall,
)
from app.deepspace.services.sandbox_backend import BwrapBackend

logger = logging.getLogger(__name__)

_REQUEST_MARKER = "__PTC_REQUEST__"
_RESPONSE_MARKER = "__PTC_RESPONSE__"
_RESULT_MARKER = "__PTC_RESULT__"


@dataclass(frozen=True, slots=True)
class NodePtcConfig:
    """Node PTC runtime configuration."""

    timeout_ms: int = 120_000
    max_timeout_ms: int = 600_000
    max_output_bytes: int = 67_108_864  # 64 MB
    max_old_generation_size_mb: int = 512
    max_message_bytes: int = 134_217_728  # 128 MB
    max_pending_calls: int = 128
    grace_ms: int = 3_000
    node_executable: str = "node"
    bootstrap_path: str | None = None


#: A host tool binding: receives the program's arguments and returns a value.
ToolBinding = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PtcRunSpec:
    """Resolved PTC run specification."""

    program: str
    bindings: dict[str, ToolBinding]
    cwd: str
    timeout_ms: int
    sandbox_policy: Any = None
    max_tool_calls: int = 24


@dataclass(slots=True)
class _ChannelState:
    """Mutable state the stdout reader and tool answers share."""

    output: list[str] = field(default_factory=list)
    tool_calls: list[ProgramToolCall] = field(default_factory=list)
    value: Any = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class NodePtcRuntime:
    """Executes JavaScript programs with host tool bindings."""

    language = "javascript"
    isolation = "process"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._config = self._load_config(settings)
        self._disposed = False

    def _load_config(self, settings: Settings) -> NodePtcConfig:
        """Load configuration from settings."""
        return NodePtcConfig(
            timeout_ms=int(getattr(settings, "deepspace_ptc_node_timeout_ms", 120_000)),
            max_timeout_ms=int(getattr(settings, "deepspace_ptc_node_max_timeout_ms", 600_000)),
            max_output_bytes=int(
                getattr(settings, "deepspace_ptc_node_max_output_bytes", 67_108_864)
            ),
            max_old_generation_size_mb=int(
                getattr(settings, "deepspace_ptc_node_max_old_generation_mb", 512)
            ),
            max_message_bytes=int(
                getattr(settings, "deepspace_ptc_node_max_message_bytes", 134_217_728)
            ),
            max_pending_calls=int(getattr(settings, "deepspace_ptc_node_max_pending_calls", 128)),
            grace_ms=int(getattr(settings, "deepspace_ptc_node_grace_ms", 3_000)),
            node_executable=str(getattr(settings, "deepspace_ptc_node_executable", "node")),
            bootstrap_path=getattr(settings, "deepspace_ptc_node_bootstrap_path", None),
        )

    @property
    def execution_instructions(self) -> str:
        """Describe the execution environment to the model."""
        return (
            "Each call runs in a fresh Node.js process inside a bubblewrap cage. "
            "Node built-ins are available through require(...) or await import(...). "
            "Relative paths use the supplied working directory; process.env starts "
            "minimal. Only the tools named in the request are callable through "
            "await tool(name, arguments)."
        )

    def resolve(
        self,
        program: str,
        bindings: dict[str, ToolBinding],
        cwd: str,
        timeout_ms: int | None,
        sandbox_policy: Any,
        max_tool_calls: int = 24,
    ) -> PtcRunSpec:
        """Resolve one execution request.

        Args:
            program: The JavaScript program body.
            bindings: Host tools the program may call, by name.
            cwd: The workspace the program runs in.
            timeout_ms: The wall-clock budget, or the configured default.
            sandbox_policy: The caller's policy, recorded for diagnostics.
            max_tool_calls: The most host tools the program may call.

        Returns:
            The resolved run specification.

        Raises:
            RuntimeError: The runtime has been disposed.
        """
        if self._disposed:
            raise RuntimeError("PTC runtime disposed")
        return PtcRunSpec(
            program=program,
            bindings=bindings,
            cwd=cwd,
            timeout_ms=min(int(timeout_ms or self._config.timeout_ms), self._config.max_timeout_ms),
            sandbox_policy=sandbox_policy,
            max_tool_calls=max_tool_calls,
        )

    async def run(self, spec: PtcRunSpec) -> ProgramOutcome:
        """Run one JavaScript program and answer its tool calls.

        Args:
            spec: The resolved program, bindings, workspace, and budget.

        Returns:
            The program's output, return value, and tool-call ledger.
        """
        if self._disposed:
            return ProgramOutcome(status="failed", error="Runtime disposed")

        node_binary = shutil.which(self._config.node_executable)
        if node_binary is None:
            return ProgramOutcome(
                status="unavailable",
                error=(f"Node.js executable {self._config.node_executable!r} was not found."),
            )
        workspace = Path(spec.cwd)
        if not workspace.is_dir():
            return ProgramOutcome(
                status="failed", error=f"The workspace {spec.cwd!r} does not exist."
            )
        sandbox = BwrapBackend()
        if not sandbox.available():
            return ProgramOutcome(
                status="unavailable",
                error=(
                    "The JavaScript runtime needs bubblewrap. Install bubblewrap "
                    "(bwrap) and try again."
                ),
            )

        with tempfile.TemporaryDirectory(prefix="deepspace-ptc-") as tmpdir:
            directory = Path(tmpdir)
            program_file = directory / "program.js"
            program_file.write_text(self._wrap_program(spec.program), encoding="utf-8")
            bootstrap_file = directory / "bootstrap.js"
            if self._config.bootstrap_path and Path(self._config.bootstrap_path).exists():
                shutil.copy(self._config.bootstrap_path, bootstrap_file)
            else:
                bootstrap_file.write_text(self._default_bootstrap(), encoding="utf-8")

            argv = [node_binary, str(program_file)]
            node_root = Path(node_binary).resolve().parent.parent
            command = sandbox.wrap(
                argv,
                workspace=workspace,
                cwd=workspace,
                network=False,
                extra_read_only_paths=(
                    str(directory),
                    str(node_root),
                    str(node_root.parent),
                ),
            )
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env={
                    **os.environ,
                    "NODE_OPTIONS": (
                        f"--max-old-space-size={self._config.max_old_generation_size_mb}"
                    ),
                    "DEEPSPACE_PTC_WORKSPACE": str(workspace),
                    "DEEPSPACE_PTC_TIMEOUT_MS": str(spec.timeout_ms),
                    "DEEPSPACE_PTC_MAX_OUTPUT": str(self._config.max_output_bytes),
                },
                limit=self._config.max_message_bytes,
            )

            state = _ChannelState()
            started = time.monotonic()
            stderr_parts: list[str] = []

            async def answer(request: dict[str, Any]) -> None:
                """Run one requested host tool and answer the program."""
                name = str(request.get("name") or "")
                raw_arguments = request.get("arguments")
                arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
                call = ProgramToolCall(name=name, arguments=arguments, status="refused")
                value: Any = None
                if len(state.tool_calls) >= spec.max_tool_calls:
                    call.error = f"This program may call at most {spec.max_tool_calls} host tools."
                elif name not in spec.bindings:
                    call.error = f"The tool {name!r} is not available to this program."
                else:
                    try:
                        value = await spec.bindings[name](arguments)
                    except Exception as exc:  # noqa: BLE001 - reported to the program
                        call.status = "failed"
                        call.error = str(exc)[:1_000] or exc.__class__.__name__
                    else:
                        call.status = "ok"
                        call.error = None
                state.tool_calls.append(call)
                message = {
                    "id": request.get("id"),
                    "ok": call.status == "ok",
                    "result": value if call.status == "ok" else None,
                    "error": call.error,
                }
                await self._send(process, message, state.write_lock)

            async def serve() -> None:
                """Read program output and host-tool requests until EOF."""
                assert process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if text.startswith(_REQUEST_MARKER):
                        try:
                            request = json.loads(text[len(_REQUEST_MARKER) :])
                        except ValueError:
                            state.output.append(text + "\n")
                            continue
                        await answer(request)
                        continue
                    if text.startswith(_RESULT_MARKER):
                        try:
                            state.value = json.loads(text[len(_RESULT_MARKER) :])
                        except ValueError:
                            state.value = None
                        continue
                    state.output.append(text + "\n")

            async def drain_stderr() -> None:
                """Keep stderr captured so a full pipe cannot block Node."""
                assert process.stderr is not None
                while True:
                    chunk = await process.stderr.read(65_536)
                    if not chunk:
                        return
                    stderr_parts.append(chunk.decode("utf-8", errors="replace"))

            servers = [
                asyncio.create_task(serve()),
                asyncio.create_task(drain_stderr()),
            ]
            status = "ok"
            error: str | None = None
            try:
                await asyncio.wait_for(asyncio.gather(*servers), timeout=spec.timeout_ms / 1000)
                await asyncio.wait_for(process.wait(), timeout=self._config.grace_ms / 1000)
            except TimeoutError:
                status = "timeout"
                error = f"Execution timed out after {spec.timeout_ms}ms"
            finally:
                for task in servers:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=5)

            stderr_text = "".join(stderr_parts)[: self._config.max_output_bytes].strip()
            if status == "ok" and process.returncode not in (0, None):
                status = "failed"
                error = f"Process exited with code {process.returncode}" + (
                    f": {stderr_text}" if stderr_text else ""
                )
            elif status == "ok" and stderr_text and not state.value:
                error = stderr_text
            return ProgramOutcome(
                status=status,
                output="".join(state.output)[: self._config.max_output_bytes],
                value=state.value,
                error=error,
                tool_calls=state.tool_calls,
                duration_ms=int((time.monotonic() - started) * 1000),
                sandboxed=command.sandboxed,
                sandbox_backend=command.backend,
            )

    @staticmethod
    async def _send(
        process: asyncio.subprocess.Process,
        message: dict[str, Any],
        write_lock: asyncio.Lock,
    ) -> None:
        """Write one framed response to the program's stdin.

        Args:
            process: The running Node process.
            message: The response payload.
            write_lock: Serializes concurrent tool answers.
        """
        if process.stdin is None:
            return
        frame = _RESPONSE_MARKER + json.dumps(message, ensure_ascii=False, default=str)
        async with write_lock:
            try:
                process.stdin.write(frame.encode("utf-8") + b"\n")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return

    def _wrap_program(self, program: str) -> str:
        """Wrap the program body in the bootstrap entry point."""
        return f"""
const {{ run }} = require('./bootstrap.js');

run(async ({{ tool }}) => {{
{program}
}});
"""

    def _default_bootstrap(self) -> str:
        """Return the bootstrap that provides ``tool`` and reports the result."""
        return f"""
'use strict';
const readline = require('readline');

let sequence = 0;
const pending = new Map();
const input = readline.createInterface({{ input: process.stdin }});

input.on('line', (line) => {{
  if (!line.startsWith({json.dumps(_RESPONSE_MARKER)})) return;
  let message;
  try {{
    message = JSON.parse(line.slice({json.dumps(_RESPONSE_MARKER)}.length));
  }} catch (error) {{
    return;
  }}
  const call = pending.get(message.id);
  if (!call) return;
  pending.delete(message.id);
  if (message.ok) call.resolve(message.result);
  else call.reject(new Error(message.error || 'The tool call failed.'));
}});

function tool(name, args = {{}}) {{
  const id = `call_${{++sequence}}`;
  return new Promise((resolve, reject) => {{
    pending.set(id, {{ resolve, reject }});
    process.stdout.write(
      {json.dumps(_REQUEST_MARKER)} +
        JSON.stringify({{ id, name, arguments: args }}) +
        '\\n',
    );
  }});
}}

function writeFrame(stream, frame) {{
  return new Promise((resolve) => stream.write(frame, resolve));
}}

async function run(program) {{
  let exitCode = 0;
  try {{
    const value = await program({{ tool }});
    await writeFrame(
      process.stdout,
      {json.dumps(_RESULT_MARKER)} +
        JSON.stringify(value === undefined ? null : value) +
        '\\n',
    );
  }} catch (error) {{
    exitCode = 1;
    await writeFrame(
      process.stderr,
      (error && error.stack ? error.stack : String(error)) + '\\n',
    );
  }} finally {{
    input.close();
  }}
  process.exit(exitCode);
}}

module.exports = {{ run }};
"""


def create_node_ptc_runtime(settings: Settings) -> NodePtcRuntime:
    """Create a Node PTC runtime for the deployment settings."""
    return NodePtcRuntime(settings)


__all__ = [
    "NodePtcConfig",
    "NodePtcRuntime",
    "PtcRunSpec",
    "create_node_ptc_runtime",
]
