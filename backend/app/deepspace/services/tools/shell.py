"""Local shell tools for DeepSpace.

These tools run a real command in the worker process.  Two things bound that
power: the deployment's permission mode decides how much of the host a command
may reach, and every run has a timeout and an output cap so a runaway command
cannot stall a worker or flood the model's context.

Two layers bound a command.  The permission mode inspects the command line:
``read-only`` refuses a command whose leading word plainly mutates state;
``workspace-write`` starts the command inside the tenant workspace and reports
a command that clearly targets a path outside it; ``danger-full-access`` skips
the inspection.  The sandbox backend then runs the command inside an
operating-system cage when the host offers one — bubblewrap on Linux, the
seatbelt sandbox on macOS, or Docker when an image is configured.  The cage
hides the user's home directory and makes the workspace the only writable
path.  ``deepspace_sandbox_required`` turns a missing backend into a refusal
instead of a silent fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any, cast

from pydantic import Field

from app.deepspace.services.sandbox_backend import (
    SandboxCommand,
    SandboxUnavailableError,
    build_sandbox_command,
    runner_options,
)
from app.deepspace.services.terminals import ShellKind, build_session_environment
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    string_property,
)
from app.deepspace.services.workspace_root import (
    WorkspacePathError,
    resolve_within_root,
    workspace_root_for,
)

#: Commands that plainly mutate state, used by the read-only permission mode.
MUTATING_COMMANDS = frozenset(
    {
        "apt",
        "apt-get",
        "chgrp",
        "chmod",
        "chown",
        "cp",
        "curl",
        "dd",
        "docker",
        "git",
        "init",
        "install",
        "ln",
        "mkdir",
        "mkfs",
        "mv",
        "npm",
        "pip",
        "pip3",
        "powershell",
        "pwsh",
        "rm",
        "rmdir",
        "rsync",
        "sed",
        "shred",
        "shutdown",
        "sudo",
        "tee",
        "touch",
        "truncate",
        "wget",
        "winget",
        "yum",
    }
)

#: Shell programs that are only mutating when they carry a write flag.
CONDITIONALLY_MUTATING = frozenset({"sed", "find", "xargs", "awk"})

logger = logging.getLogger(__name__)


class BashArguments(ToolArguments):
    """Run a shell command in the project workspace."""

    command: str = Field(min_length=1, max_length=20_000)
    cwd: str | None = Field(default=None, max_length=1024)
    timeout_seconds: int | None = Field(default=None, ge=1, le=900)


class PowerShellArguments(ToolArguments):
    """Run a PowerShell command in the project workspace."""

    command: str = Field(min_length=1, max_length=20_000)
    cwd: str | None = Field(default=None, max_length=1024)
    timeout_seconds: int | None = Field(default=None, ge=1, le=900)


def _leading_command(command: str) -> str:
    """Return the leading program name of a shell command.

    Args:
        command: The command line to inspect.

    Returns:
        The first word, lowercased, without a directory prefix.
    """
    tokens = command.strip().split()
    if not tokens:
        return ""
    first = tokens[0]
    for separator in ("|", ";", "&&", "||", ">", ">>"):
        first = first.split(separator, 1)[0]
    return Path(first.strip()).name.lower()


def read_only_violation(command: str) -> str | None:
    """Report why a command is refused under the read-only permission mode.

    Args:
        command: The command line to inspect.

    Returns:
        The leading mutating program, or None when the command passes the
        inspection.
    """
    leading = _leading_command(command)
    if not leading:
        return None
    if leading in MUTATING_COMMANDS:
        return leading
    if leading in CONDITIONALLY_MUTATING:
        lowered = command.lower()
        if leading == "sed" and ("-i" in lowered or "--in-place" in lowered):
            return leading
        if leading == "find" and (" -delete" in lowered or " -exec" in lowered):
            return leading
        if leading == "xargs" and ("rm" in lowered or "mv" in lowered):
            return leading
        if leading == "awk" and (">>" in lowered or "system(" in lowered):
            return leading
    return None


def _shell_program(kind: str) -> str | None:
    """Resolve the shell program for one tool kind.

    Args:
        kind: Either ``bash`` or ``pwsh``.

    Returns:
        The absolute program path, or None when the platform has no such
        shell.
    """
    if kind == "bash":
        for candidate in ("/bin/bash", "/usr/bin/bash"):
            if os.path.exists(candidate):
                return candidate
        return "/bin/sh" if os.path.exists("/bin/sh") else None
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found is not None:
            return found
    return None


def _working_directory(context: ToolContext, raw_cwd: str | None) -> tuple[Path, Path, str]:
    """Resolve the workspace and directory a command runs in.

    The conversation's own workspace wins when the request resolved one, so
    a command runs in the tree the conversation works in.  Without one the
    account's default workspace is the root.

    Args:
        context: The calling request's tool context.
        raw_cwd: The optional workspace-relative directory.

    Returns:
        The workspace root, the absolute directory, and its workspace-relative
        form.
    """
    root = context.workspace_root
    if root is None:
        root = workspace_root_for(context.settings, context.auth.tenant_id, context.auth.user_id)
    if not raw_cwd:
        return root, root, ""
    resolved = resolve_within_root(root, raw_cwd)
    return root, resolved.absolute, resolved.relative


async def _run_command(
    *,
    kind: str,
    command: str,
    workspace: Path,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    settings: Any,
) -> dict[str, Any]:
    """Run one command and collect its bounded output.

    Args:
        kind: The shell kind, ``bash`` or ``pwsh``.
        command: The command line to run.
        workspace: The only writable directory the command may see.
        cwd: The directory to run in.
        timeout_seconds: The wall-clock budget for the command.
        max_output_bytes: The largest combined output the result may carry.
        settings: The deployment settings that choose the sandbox backend.

    Returns:
        The exit code, the captured output, the sandbox that ran it, and
        whether the run was cut short.
    """
    program = _shell_program(kind)
    if program is None:
        return {
            "status": "unavailable",
            "message": f"No {kind} shell is available on this platform.",
        }
    native_argv = [program, "-c" if kind == "bash" else "-Command", command]
    policy = str(
        getattr(settings, "deepspace_sandbox_policy", None)
        or getattr(settings, "deepspace_permission_mode", "workspace-write")
    )
    bypass_sandbox = policy == "danger-full-access"
    sandbox = (
        SandboxCommand(argv=native_argv, backend="none", sandboxed=False)
        if bypass_sandbox
        else build_sandbox_command(
            settings,
            native_argv,
            workspace=workspace,
            cwd=cwd,
            read_only=policy == "read-only",
        )
    )
    if (
        not sandbox.sandboxed
        and not bypass_sandbox
        and bool(getattr(settings, "deepspace_sandbox_required", False))
    ):
        # The command is refused rather than run without containment, and
        # the failure carries the typed code and the runners this platform
        # offers, so the operator knows what to install.
        unavailable = SandboxUnavailableError(
            platform=sys.platform,
            options=runner_options(),
        )
        return {
            "status": "unavailable",
            "error_code": "SANDBOX_UNAVAILABLE",
            "message": str(unavailable),
            "runner_options": list(unavailable.options),
            "sandbox_backend": sandbox.backend,
            "sandboxed": False,
        }
    process = await asyncio.create_subprocess_exec(
        *sandbox.argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=build_session_environment(
            workspace=workspace,
            shell=cast(ShellKind, kind),
            mode=policy,
        ),
    )
    captured = bytearray()
    truncated = False
    dropped_bytes = 0

    async def _drain(stream: asyncio.StreamReader | None) -> None:
        nonlocal truncated
        nonlocal dropped_bytes
        if stream is None:
            return
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                return
            if len(captured) + len(chunk) > max_output_bytes:
                remaining = max(0, max_output_bytes - len(captured))
                captured.extend(chunk[:remaining])
                dropped_bytes += len(chunk) - remaining
                truncated = True
                # The rest is kept on disk by the caller, so the drain stops
                # reading rather than losing the output entirely.
                return
            captured.extend(chunk)

    try:
        await asyncio.wait_for(
            asyncio.gather(_drain(process.stdout), _drain(process.stderr)),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        _terminate(process)
        _release_pipes(process)
        return {
            "status": "timeout",
            "message": f"The command did not finish within {timeout_seconds} seconds.",
            "output": captured.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "sandbox_backend": sandbox.backend,
            "sandboxed": sandbox.sandboxed,
        }
    except asyncio.CancelledError:
        # The call was cancelled from the outside, so the command's process
        # is stopped the way the shell's own timeout stops it.  The
        # cancellation is passed on, so the caller still sees it.
        _terminate(process)
        _release_pipes(process)
        raise
    exit_code = await process.wait()
    output = captured.decode("utf-8", errors="replace")
    failure_kind = classify_failure(
        output=output,
        exit_code=exit_code,
        backend=sandbox.backend,
    )
    return {
        "status": "ok" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "output": output,
        "truncated": truncated,
        "sandbox_backend": sandbox.backend,
        "sandboxed": sandbox.sandboxed,
        **({"failure_kind": failure_kind} if failure_kind else {}),
    }


#: The stderr signature each runner prints when it fails before the command
#: starts.  A runner failure is not a policy denial, and a caller that
#: cannot tell them apart will report a broken sandbox as a refused command.
RUNNER_FAILURE_SIGNATURES: dict[str, str] = {
    "bwrap": "bwrap: ",
    "landlock": "landlock-run:",
    "windows-acl": "windows-acl-run:",
    "docker": "docker: ",
}

#: The exit codes the runners use for their own failure.
RUNNER_FAILURE_EXIT_CODES = frozenset({125, 127})

#: The signatures a kernel or a file system uses when it refuses an access.
DENIAL_SIGNATURES = (
    "permission denied",
    "operation not permitted",
    "access is denied",
    "read-only file system",
)


def classify_failure(*, output: str, exit_code: int, backend: str) -> str | None:
    """Classify why a command failed.

    The classification separates a policy denial from a runner failure.  A
    denial is the sandbox doing its job; a runner failure is the sandbox
    itself being broken, which a caller must not report as a refused
    command.

    Args:
        output: The command's combined output.
        exit_code: The command's exit code.
        backend: The backend that ran the command.

    Returns:
        ``denied`` for a policy denial, ``runner_failure`` for a broken
        sandbox, and None when the command simply failed.
    """
    text = output or ""
    signature = RUNNER_FAILURE_SIGNATURES.get(backend)
    if signature and signature in text:
        return "runner_failure"
    if backend in {"bwrap", "landlock", "windows-acl"} and exit_code in RUNNER_FAILURE_EXIT_CODES:
        return "runner_failure"
    lowered = text.lower()
    if any(marker in lowered for marker in DENIAL_SIGNATURES):
        return "denied"
    return None


def _terminate(process: asyncio.subprocess.Process) -> None:
    """Stop a timed-out command and its process group.

    Args:
        process: The running command.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            return


def _release_pipes(process: asyncio.subprocess.Process) -> None:
    """Close the pipes of a stopped command.

    The command is already killed, so its pipes are closed rather than left
    for the garbage collector.

    Args:
        process: The stopped command.
    """
    transport = getattr(process, "_transport", None)
    close = getattr(transport, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.warning("The shell command's pipes could not be closed", exc_info=True)


def _effective_mode(context: ToolContext) -> str:
    """Return the containment mode the shell tools must obey.

    The conversation's own mode wins when it has one, so a user can tighten
    one conversation without changing the deployment.  Otherwise
    ``deepspace_sandbox_policy`` owns shell containment, and
    ``deepspace_permission_mode`` remains the deployment-wide default.

    Args:
        context: The calling request's tool context.

    Returns:
        ``read-only``, ``workspace-write``, or ``danger-full-access``.
    """
    conversation_mode = str(context.sandbox_mode or "").strip().lower()
    if conversation_mode:
        return conversation_mode
    return str(
        getattr(context.settings, "deepspace_sandbox_policy", None)
        or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
    )


def _permission_decision(context: ToolContext, command: str) -> dict[str, Any] | None:
    """Apply the deployment's permission mode to one command.

    Args:
        context: The calling request's tool context.
        command: The command line to inspect.

    Returns:
        A refusal result when the command does not pass, otherwise None.
    """
    mode = _effective_mode(context)
    if mode == "danger-full-access":
        return None
    if mode == "read-only":
        violation = read_only_violation(command)
        if violation is not None:
            return {
                "status": "denied",
                "message": (
                    f"The '{violation}' command changes state and this deployment runs in "
                    "read-only permission mode."
                ),
                "permission_mode": mode,
            }
    return None


async def _run_shell(
    *,
    kind: str,
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    """Run one shell command under the deployment's permission mode."""
    if not bool(getattr(context.settings, "deepspace_shell_enabled", True)):
        return {
            "status": "unavailable",
            "message": "The local shell tools are disabled on this deployment.",
        }
    command = str(arguments["command"])
    refusal = _permission_decision(context, command)
    if refusal is not None:
        return refusal
    try:
        workspace, cwd, relative_cwd = _working_directory(context, arguments.get("cwd"))
    except WorkspacePathError as exc:
        return {"status": "error", "message": str(exc)}
    requested_timeout = arguments.get("timeout_seconds")
    timeout_seconds = int(
        requested_timeout
        if requested_timeout is not None
        else getattr(context.settings, "deepspace_shell_timeout_seconds", 120)
    )
    # The whole output is kept. A large one is spilled to durable storage, so
    # the model reads it back in pages instead of losing what did not fit.
    capture_bytes = int(getattr(context.settings, "deepspace_shell_capture_bytes", 8_000_000))
    result = await _run_command(
        kind=kind,
        command=command,
        workspace=workspace,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_bytes=capture_bytes,
        settings=context.settings,
    )
    result["command"] = command
    result["cwd"] = relative_cwd or "."
    result["shell"] = kind
    result["permission_mode"] = _effective_mode(context)
    result = _spill_large_output(result, context=context, tool_name=kind)
    if context.cancel_check is not None and context.cancel_check():
        result["cancelled"] = True
    return result


def _spill_large_output(
    result: dict[str, Any], *, context: ToolContext, tool_name: str
) -> dict[str, Any]:
    """Keep a large command output instead of discarding it.

    A build that prints more than the model context holds is written to
    durable storage, and the model receives a preview plus the reference it
    reads back with ``spill_read``. Nothing is lost.

    Args:
        result: The command's result.
        context: The calling request's tool context.
        tool_name: The tool the result belongs to.

    Returns:
        The result the model receives.
    """
    output = str(result.get("output") or "")
    threshold = int(getattr(context.settings, "deepspace_spill_threshold_chars", 24_000))
    if len(output) <= threshold:
        return result
    from app.deepspace.services.spill_store import SpillStoreError, spill_result

    try:
        spilled, reference = spill_result(
            settings=context.settings,
            tenant_id=context.auth.tenant_id,
            tool_name=tool_name,
            payload=result,
            threshold_chars=threshold,
            max_chars=int(getattr(context.settings, "deepspace_shell_capture_bytes", 8_000_000)),
        )
    except SpillStoreError:
        # The output is larger than the deployment can store. The preview
        # stays, and the result says how much was kept.
        result["truncated"] = True
        return result
    except Exception:  # noqa: BLE001
        return result
    if reference is None:
        return result
    spilled["command"] = result.get("command")
    spilled["cwd"] = result.get("cwd")
    spilled["shell"] = result.get("shell")
    spilled["exit_code"] = result.get("exit_code")
    spilled["dropped_bytes"] = result.get("dropped_bytes", 0)
    return spilled


async def _bash(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run a command through the POSIX shell."""
    return await _run_shell(kind="bash", arguments=arguments, context=context)


async def _pwsh(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run a command through PowerShell."""
    return await _run_shell(kind="pwsh", arguments=arguments, context=context)


BASH_DESCRIPTION = (
    "Run a shell command in the project workspace and return its exit code and "
    "output. Use it for builds, tests, and version control; use fs_read and "
    "fs_write for single files."
)
PWSH_DESCRIPTION = (
    "Run a PowerShell command in the project workspace and return its exit code "
    "and output. Available on Windows deployments."
)

SHELL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="bash",
        description=BASH_DESCRIPTION,
        properties={
            "command": string_property("The command line to run.", min_length=1, max_length=20_000),
            "cwd": string_property(
                "Workspace-relative directory to run in. Defaults to the workspace root.",
                min_length=1,
                max_length=1024,
            ),
            "timeout_seconds": integer_property(
                "Wall-clock budget for the command.", minimum=1, maximum=900
            ),
        },
        required=["command"],
        arguments_model=BashArguments,
        mode="write",
        handler=_bash,
    ),
    define_native_tool(
        name="pwsh",
        description=PWSH_DESCRIPTION,
        properties={
            "command": string_property("The command line to run.", min_length=1, max_length=20_000),
            "cwd": string_property(
                "Workspace-relative directory to run in. Defaults to the workspace root.",
                min_length=1,
                max_length=1024,
            ),
            "timeout_seconds": integer_property(
                "Wall-clock budget for the command.", minimum=1, maximum=900
            ),
        },
        required=["command"],
        arguments_model=PowerShellArguments,
        mode="write",
        handler=_pwsh,
    ),
]

__all__ = [
    "BashArguments",
    "DENIAL_SIGNATURES",
    "PowerShellArguments",
    "RUNNER_FAILURE_EXIT_CODES",
    "RUNNER_FAILURE_SIGNATURES",
    "SHELL_TOOLS",
    "classify_failure",
    "read_only_violation",
]
