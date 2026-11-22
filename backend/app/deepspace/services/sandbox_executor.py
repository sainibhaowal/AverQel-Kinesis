"""Tenant-safe client for remote or local isolated code execution.

Desktop builds execute in a private child process inside the local OS cage;
server deployments can use a separately deployed, network-isolated executor.
Both paths retain the same input limits and tenant authorization boundary.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.deepspace.services.sandbox_backend import build_sandbox_command
from app.deepspace.services.subprocess_stdio import (
    SubprocessCollect,
    SubprocessStdio,
    spawn_with_stdio,
)

SUPPORTED_LANGUAGES = {"python", "sql", "bash", "pwsh"}
MAX_CODE_BYTES = 100_000
MAX_INPUT_BYTES = 2_000_000
MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILE_BUNDLE_BYTES = 20 * 1024 * 1024
MAX_GENERATED_FILE_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 200_000
MAX_GENERATED_TOTAL_BYTES = 1_500_000


@dataclass(frozen=True, slots=True)
class SandboxResult:
    status: str
    language: str
    stdout: str
    stderr: str
    duration_ms: int
    files: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "language": self.language,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "files": self.files,
            "execution": "isolated_sandbox",
        }


class SandboxExecutorError(RuntimeError):
    """A safe, user-displayable execution error."""


async def execute_sandbox(
    *,
    code: str,
    language: str,
    settings: Any,
    input_data: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    normalized_language = language.strip().lower()
    if normalized_language not in SUPPORTED_LANGUAGES:
        raise SandboxExecutorError(
            "Only Python, read-only SQL, Bash, and PowerShell execution is supported."
        )
    if not isinstance(code, str) or not code.strip():
        raise SandboxExecutorError("Execution code is required.")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise SandboxExecutorError("Execution code exceeds the 100 KB safety limit.")
    payload = input_data if isinstance(input_data, dict) else {}
    if len(str(payload).encode("utf-8")) > MAX_INPUT_BYTES:
        raise SandboxExecutorError("Execution input exceeds the 2 MB safety limit.")
    safe_files: list[dict[str, Any]] = []
    bundle_bytes = 0
    for item in files or []:
        if not isinstance(item, dict):
            raise SandboxExecutorError("Sandbox file entries must be objects.")
        name = str(item.get("name") or "").strip()
        data = item.get("data_base64")
        if not name or not isinstance(data, str):
            raise SandboxExecutorError("Sandbox files require a name and binary payload.")
        try:
            decoded_size = len(base64.b64decode(data, validate=True))
        except (ValueError, TypeError) as exc:
            raise SandboxExecutorError("Sandbox file payload is invalid.") from exc
        if decoded_size > MAX_FILE_BYTES:
            raise SandboxExecutorError("A sandbox file exceeds the 10 MB safety limit.")
        bundle_bytes += decoded_size
        if bundle_bytes > MAX_FILE_BUNDLE_BYTES:
            raise SandboxExecutorError("Sandbox files exceed the 20 MB aggregate safety limit.")
        safe_files.append(
            {
                "name": name[:255],
                "content_type": str(item.get("content_type") or "application/octet-stream")[:127],
                "data_base64": data,
                "extracted_text": str(item.get("extracted_text") or "")[:200_000],
            }
        )
    if len(safe_files) > MAX_FILES:
        raise SandboxExecutorError(f"At most {MAX_FILES} Library files may be analyzed at once.")
    if not bool(getattr(settings, "deepspace_sandbox_enabled", False)):
        raise SandboxExecutorError("The isolated sandbox is not enabled on this deployment.")
    endpoint = str(getattr(settings, "deepspace_sandbox_url", "") or "").rstrip("/")
    if not endpoint:
        return await _execute_locally(
            code=code,
            language=normalized_language,
            input_data=payload,
            files=safe_files,
            settings=settings,
            timeout_seconds=timeout_seconds,
        )
    token = str(getattr(settings, "deepspace_sandbox_token", "") or "")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = {
        "language": normalized_language,
        "code": code,
        "input": payload,
        "files": safe_files,
        "timeout_seconds": max(1, min(30, int(timeout_seconds))),
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(35.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(f"{endpoint}/execute", json=request, headers=headers)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SandboxExecutorError("The isolated sandbox could not complete the request.") from exc
    if not isinstance(data, dict):
        raise SandboxExecutorError("The isolated sandbox returned an invalid response.")
    output_files: list[dict[str, Any]] = []
    output_total = 0
    raw_output_files = data.get("files")
    for item in raw_output_files if isinstance(raw_output_files, list) else []:
        if not hasattr(item, "get"):
            continue
        encoded = item.get("data_base64")
        if not isinstance(encoded, str):
            continue
        try:
            decoded_size = len(base64.b64decode(encoded, validate=True))
        except (ValueError, TypeError):
            continue
        if decoded_size <= 0 or decoded_size > MAX_GENERATED_FILE_BYTES:
            continue
        output_total += decoded_size
        if output_total > MAX_GENERATED_TOTAL_BYTES:
            break
        output_files.append(
            {
                "name": str(item.get("name") or "output")[:255],
                "content_type": str(item.get("content_type") or "text/plain")[:127],
                "data_base64": encoded,
                "size_bytes": decoded_size,
            }
        )
    return SandboxResult(
        status=str(data.get("status") or "failed"),
        language=normalized_language,
        stdout=str(data.get("stdout") or "")[:200_000],
        stderr=str(data.get("stderr") or "")[:50_000],
        duration_ms=max(0, int(data.get("duration_ms") or 0)),
        files=output_files[:10],
    ).as_dict()


def _local_command(language: str, script_path: Path) -> tuple[list[str], tuple[str, ...]]:
    """Select an interpreter and only the runtime paths it needs to read."""
    if language in {"python", "sql"}:
        executable: str | None
        if getattr(sys, "frozen", False):
            executable = sys.executable
            command = [executable, "--deepspace-sandbox-python", str(script_path)]
            runtime_roots = tuple(
                str(path)
                for path in {Path(executable).parent, Path(getattr(sys, "_MEIPASS", executable))}
            )
        else:
            executable = next(
                (
                    item
                    for item in ("/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3")
                    if Path(item).is_file()
                ),
                None,
            )
            if executable is None:
                executable = shutil.which("python3")
            if not executable:
                raise SandboxExecutorError(
                    "A system Python 3 interpreter is required for local sandbox execution."
                )
            command = [executable, "-I", "-S", str(script_path)]
            runtime_roots = ()
        return command, runtime_roots
    shell = shutil.which("bash" if language == "bash" else "pwsh")
    if not shell:
        raise SandboxExecutorError(f"{language} is not installed for local sandbox execution.")
    if language == "bash":
        return [shell, "--noprofile", "--norc", str(script_path)], ()
    return [shell, "-NoProfile", "-NonInteractive", "-File", str(script_path)], ()


def _sql_program(query: str, tables: dict[str, Any]) -> str:
    """Build a read-only in-memory SQLite query program with an authorizer."""
    payload = json.dumps(tables, ensure_ascii=False, default=str)
    return f"""import json, sqlite3
tables = json.loads({payload!r})
db = sqlite3.connect(":memory:")
for raw_name, rows in tables.items():
    if not isinstance(raw_name, str) or not isinstance(rows, list) or not rows:
        continue
    columns = list(rows[0]) if isinstance(rows[0], dict) else []
    name = "".join(c for c in raw_name if c.isalnum() or c == "_")
    columns = ["".join(c for c in str(c) if c.isalnum() or c == "_") for c in columns]
    if not name or not columns or any(not c for c in columns):
        continue
    q = lambda value: '"' + value.replace('"', '""') + '"'
    db.execute("create table " + q(name) + " (" + ",".join(q(c) + " text" for c in columns) + ")")
    db.executemany("insert into " + q(name) + " values (" + ",".join("?" for _ in columns) + ")",
                   [[row.get(key) for key in (list(rows[0]) if isinstance(rows[0], dict) else [])]
                    for row in rows if isinstance(row, dict)])
def authorize(action, arg1, arg2, database, trigger):
    allowed = {{sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
db.set_authorizer(authorize)
try:
    result = db.execute({query.strip()!r}).fetchall()
except sqlite3.DatabaseError as exc:
    raise SystemExit("SQL rejected: " + str(exc))
print(json.dumps(result, ensure_ascii=False, default=str))
"""


async def _execute_locally(
    *,
    code: str,
    language: str,
    input_data: dict[str, Any],
    files: list[dict[str, Any]],
    settings: Any,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run one request in a private workspace inside a full OS sandbox."""
    with tempfile.TemporaryDirectory(prefix="averqel-sandbox-") as temp_root:
        workdir = Path(temp_root)
        initial_files: set[str] = set()
        for item in files:
            name = str(item["name"])
            if (
                not name
                or name in {".", ".."}
                or Path(name).name != name
                or "/" in name
                or "\\" in name
                or name in initial_files
            ):
                raise SandboxExecutorError("Sandbox input files must have unique plain filenames.")
            data = base64.b64decode(item["data_base64"], validate=True)
            (workdir / name).write_bytes(data)
            initial_files.add(name)
            extracted = str(item.get("extracted_text") or "")
            if extracted:
                extracted_name = f"{name}.extracted.txt"
                (workdir / extracted_name).write_text(extracted, encoding="utf-8")
                initial_files.add(extracted_name)
        script_name = (
            "main.py"
            if language in {"python", "sql"}
            else ("main.sh" if language == "bash" else "main.ps1")
        )
        script = _sql_program(code, input_data) if language == "sql" else code
        script_path = workdir / script_name
        script_path.write_text(script, encoding="utf-8")
        command, runtime_roots = _local_command(language, script_path)
        if sys.platform.startswith("linux"):
            limiter = shutil.which("prlimit")
            if not limiter:
                raise SandboxExecutorError(
                    "The local Linux sandbox requires util-linux prlimit for process, memory, CPU, and file-size limits."
                )
            command = [
                limiter,
                f"--cpu={max(2, min(31, timeout_seconds + 1))}",
                "--as=2147483648",
                "--nproc=64",
                "--fsize=5242880",
                "--nofile=64",
                "--",
                *command,
            ]
        elif sys.platform == "darwin":
            command = [
                "/bin/sh",
                "-c",
                'ulimit -t 31; ulimit -v 2097152; ulimit -f 5120; ulimit -n 64; exec "$@"',
                "sandbox-limits",
                *command,
            ]
        cage = build_sandbox_command(
            settings,
            command,
            workspace=workdir,
            cwd=workdir,
            network=False,
            extra_read_only_paths=runtime_roots,
        )
        if not cage.sandboxed or cage.backend not in {"bwrap", "sandbox-exec"}:
            raise SandboxExecutorError(
                "Local sandbox execution requires full filesystem and network isolation (bubblewrap on Linux or sandbox-exec on macOS)."
            )
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(workdir),
            "TMPDIR": "/tmp",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        started = time.monotonic()
        try:
            child = await spawn_with_stdio(
                cage.argv,
                cwd=str(workdir),
                env=env,
                apply_proxy_policy=False,
                stdio=SubprocessStdio(
                    stdout=SubprocessCollect(MAX_OUTPUT_BYTES), stderr=SubprocessCollect(50_000)
                ),
                spill_dir=str(workdir),
            )
            try:
                outcome = await asyncio.wait_for(
                    child.wait(), timeout=max(1, min(30, timeout_seconds))
                )
            except TimeoutError:
                await child.terminate_and_wait()
                return SandboxResult(
                    "timeout",
                    language,
                    "",
                    "Execution timed out.",
                    int((time.monotonic() - started) * 1000),
                    [],
                ).as_dict()
            stdout = child.collect_final("stdout") or {"text": ""}
            stderr = child.collect_final("stderr") or {"text": ""}
        except (OSError, RuntimeError, ValueError) as exc:
            raise SandboxExecutorError(
                "The local OS sandbox could not start the execution."
            ) from exc
        generated: list[dict[str, Any]] = []
        generated_total = 0
        for candidate in workdir.rglob("*"):
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or candidate.name in initial_files
                or candidate == script_path
            ):
                continue
            relative = candidate.relative_to(workdir).as_posix()
            if relative.startswith(".") or len(generated) >= 10:
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if (
                size <= 0
                or size > MAX_GENERATED_FILE_BYTES
                or generated_total + size > MAX_GENERATED_TOTAL_BYTES
            ):
                continue
            generated.append(
                {
                    "name": relative[:255],
                    "content_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
                    "data_base64": base64.b64encode(candidate.read_bytes()).decode("ascii"),
                    "size_bytes": size,
                }
            )
            generated_total += size
        result = SandboxResult(
            "completed" if outcome.exit_code == 0 else "failed",
            language,
            str(stdout["text"]),
            str(stderr["text"]),
            int((time.monotonic() - started) * 1000),
            generated,
        ).as_dict()
        result["sandbox_backend"] = cage.backend
        return result
