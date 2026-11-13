# 66. Subprocess Scope, Windows Job Objects, and Runner Protocol

The subprocess infrastructure (`shell.py`, `sandbox.py`, `subprocess` utilities)
provides controlled process execution with sandboxing, resource limits, and
cross-platform process tree management.

## Use case

- A tool runs a shell command (`bash`, `pwsh`) with a working directory and timeout.
- The sandbox must contain the process tree (no escaping child processes).
- Windows requires Job Objects for reliable process group termination.

## Why it matters

- Unbounded subprocesses can exhaust system resources.
- Orphaned child processes survive parent termination on Windows without Job Objects.
- The runner protocol enables streaming output and graceful cancellation.

## How it works

### Execution flow (`shell.py`)

```python
async def _bash(arguments, context):
    # 1. Resolve command and working directory
    cmd = arguments["command"]
    cwd = context.workspace_root or resolve_cwd(...)

    # 2. Build sandbox execution spec
    spec = SandboxSpec(
        command=cmd,
        cwd=cwd,
        env={**os.environ, **arguments.get("env", {})},
        timeout=arguments.get("timeout_seconds", 120),
        network=arguments.get("network", False),
        stdin=arguments.get("stdin"),
    )

    # 3. Execute via sandbox
    result = await sandbox_execute(spec, context)

    # 4. Return sanitized result
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}
```

### Sandbox backends (`sandbox.py`)

| Backend | Platform | Mechanism | Containment |
|---|---|---|---|
| `linux-namespace` | Linux | `unshare(CLONE_NEWPID|CLONE_NEWNS|CLONE_NEWNET)` | Full PID/net/mount namespace |
| `linux-seccomp` | Linux | `seccomp(SECCOMP_MODE_FILTER)` | Syscall filtering |
| `windows-job` | Windows | `CreateJobObject` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` | Process tree termination |
| `noop` | Any | Direct `subprocess.Popen` | None (dev only) |

The active backend is selected by `deepspace_sandbox_mode`:
- `"linux-namespace"` (default Linux)
- `"windows-job"` (default Windows)
- `"seccomp"` (Linux, syscall filtering only)
- `"noop"` (no containment)

### Linux namespace sandbox (`sandbox.py:LinuxNamespaceSandbox`)

```python
flags = (
    os.CLONE_NEWPID |    # Isolate PID 1 in container
    os.CLONE_NEWNS |     # Private mount table
    os.CLONE_NEWNET |    # No network (unless network=True)
    os.CLONE_NEWUTS |    # Private hostname
    os.CLONE_NEWIPC      # Private IPC
)
pid = os.fork()
if pid == 0:
    os.unshare(flags)
    # Mount proc, set up cwd, drop privileges
    os.execve(cmd[0], cmd, env)
else:
    # Parent monitors, enforces timeout, reaps
```

**Key behaviors:**
- Child becomes PID 1 in new PID namespace; all descendants are contained.
- `CLONE_NEWNET` with no network = no socket creation possible.
- Mount namespace: `MS_PRIVATE` on root, then bind-mount workspace read-only.
- Timeout enforced by parent via `waitpid` with `WNOHANG` polling.

### Windows Job Object sandbox (`sandbox.py:WindowsJobSandbox`)

```python
job = win32job.CreateJobObject(None, "")
win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, {
    "BasicLimitInformation": {
        "LimitFlags": win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                      win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
        "ActiveProcessLimit": 1,  # Only the direct child
    }
})
# Assign child process to job
win32job.AssignProcessToJobObject(job, child_handle)
```

**Key behaviors:**
- `KILL_ON_JOB_CLOSE`: When job handle closes, all processes in job are terminated.
- `ACTIVE_PROCESS_LIMIT=1`: Only the direct child allowed (no fork bombs).
- Job handle held by sandbox; closed on timeout or explicit cancel.

### Runner protocol (`subprocess` utilities)

The `subprocess` package (DeepSeek) defines a control/spawn protocol for
managed subprocess execution.  DeepSpace uses a simplified version in
`shell.py:run_subprocess`:

```python
async def run_subprocess(cmd, cwd, env, timeout, stdin, stdout_cb, stderr_cb):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Stream callbacks
    async def read_stream(stream, cb):
        while True:
            line = await stream.readline()
            if not line: break
            await cb(line.decode("utf-8", errors="replace"))

    await asyncio.gather(
        read_stream(proc.stdout, stdout_cb),
        read_stream(proc.stderr, stderr_cb),
        proc.wait(),
    )
    return proc.returncode
```

### Streaming output and cancellation

- `stdout_cb` / `stderr_cb` receive lines as they arrive (for `terminal_job` streaming).
- Cancellation: `proc.terminate()` → wait 5s → `proc.kill()`.
- On Windows Job sandbox: closing job handle terminates entire tree.

### Resource limits

| Limit | Linux namespace | Windows Job | Noop |
|---|---|---|---|
| Process tree containment | ✅ PID namespace | ✅ Job object | ❌ |
| Network isolation | ✅ NET namespace | ❌ | ❌ |
| Mount isolation | ✅ Mount namespace | ❌ | ❌ |
| Timeout enforcement | Parent polling | Job close + parent | Parent polling |
| Max processes | `RLIMIT_NPROC` | `ACTIVE_PROCESS_LIMIT` | ❌ |
| Memory limit | `RLIMIT_AS` (cgroup v2) | `JOB_OBJECT_LIMIT_PROCESS_MEMORY` | ❌ |

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_sandbox_mode` | `str` | `"linux-namespace"` / `"windows-job"` | Active sandbox backend. |
| `deepspace_shell_enabled` | `bool` | `true` | Enable `bash`/`pwsh` tools. |
| `deepspace_tool_timeout_seconds` | `int` (5-300) | `120` | Default tool timeout. |
| `deepspace_max_concurrent_tools` | `int` (1-64) | `8` | Parallel read-tool limit. |

## What to watch for

- **Linux namespace requires root or `CAP_SYS_ADMIN`** for `unshare(CLONE_NEWNS|CLONE_NEWNET)`.  The desktop build uses user namespaces (`unshare -Ur`) when available.
- **Windows Job Objects don't support nested jobs** — a process already in a job cannot create a child job.  The sandbox detects this and falls back to `noop` with a warning.
- **Network isolation**: Only `linux-namespace` with `network=false` blocks network.  Other backends rely on `network` flag passed to command (not enforced).
- **cgroup v2** for memory/CPU limits requires systemd-run or manual cgroup setup; not automatic.
- **Zombie reaping**: The sandbox parent must `waitpid` all children; use `asyncio.waitpid` or signal handler.

## Related chapters

- [The Sandbox](13-the-sandbox.md) — sandbox modes and permission model.
- [Terminal Session Lifecycle](64-terminal-session-lifecycle.md) — `terminal_job` streaming.
- [Native Subprocess, Terminal, Telemetry](51-native-subprocess-terminal-telemetry.md) — shared infrastructure.
