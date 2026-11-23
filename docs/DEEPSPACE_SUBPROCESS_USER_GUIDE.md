# DeepSpace Subprocess (Terminal & Background Jobs) — User Guide

**Date:** 2026-11-14

---

## What It Is

**Subprocess** is the engine that runs commands, terminals, and background jobs for DeepSpace. You don't call it directly — the agent uses it when you ask for:

- A persistent terminal (`terminal_open`, `terminal_write`, `terminal_read`)
- A background write (`terminal_write(..., background=true)` → `terminal_job`)
- A shell command via subagent providers (Codex, Claude Code, ACP)

It handles the gritty details: pseudo-terminals, Windows ConPTY, process trees, output buffering, signals, and cleanup — so you don't have to.

---

## What You Actually Use

| Feature | User-Facing Tool | What It Does |
|---|---|---|
| **Persistent Terminal** | `terminal_open/write/read/signal/close/list` | Long-lived shell session across turns |
| **Background Job** | `terminal_write(..., background=true)` → `terminal_job` | Run a long command in a terminal and read the write's job outcome |
| **Subagent CLI Providers** | `subagent` (with Codex/Claude Code/ACP) | Child agents that run as subprocesses |

You never call subprocess APIs directly. The agent chooses the right one.

---

## Persistent Terminals — What You Get

| Capability | Details |
|---|---|
| **Survives turns** | Open once, use all conversation |
| **True PTY** | `bash`/`pwsh`/`cmd` think they're talking to a human |
| **Bounded output** | Last 256 KiB kept (older drops off) |
| **Idle timeout** | Auto-closes after 15 min silence (configurable) |
| **Signals** | `SIGINT` (Ctrl+C), `SIGTERM`, `SIGHUP`, `SIGKILL`, `SIGTSTP`/`SIGCONT` |
| **Background writes** | `terminal_write(..., background=true)` returns a job id; read it with `terminal_job` |
| **Per-conversation isolation** | Your terminals are yours only |

### Natural Language Examples

| You Say | Agent Does |
|---|---|
| *"Open a terminal and run `npm run build`"* | `terminal_open(shell="bash", command="npm run build")` |
| *"What's the output?"* | `terminal_read(session_id="...")` |
| *"Send Ctrl+C"* | `terminal_signal(session_id="...", signal="SIGINT")` |
| *"Run this in background"* | `terminal_write(session_id="...", data="./build.sh\n", background=true)` → returns `job_id` |
| *"Check the background job"* | `terminal_job(job_id="...")` |
| *"Close it"* | `terminal_close(session_id="...")` |
| *"List my terminals"* | `terminal_list()` |

---

## Background Writes — Fire and Forget

```
User: "Run the test suite in background"
Agent: terminal_write(session_id="...", data="pytest\n", background=true) → job_id: "369afb21..."

... later ...

User: "How's the build?"
Agent: terminal_job(job_id="369afb21...") → status and wait_reason
       terminal_read(session_id="...") → collected output
```

**Key points:**
- The write runs as a background job in the same conversation-owned terminal.
- Output is bounded at 256 KiB; older bytes drop off.
- Read the job with `terminal_job`; read the output with `terminal_read`.
- Stop a running command with `terminal_signal` on its session.

---

## Windows Support — It Just Works

| Feature | How |
|---|---|
| **ConPTY** | `pywinpty` + contained host subprocess — true pseudo-terminal |
| **Process Groups** | `CREATE_NEW_PROCESS_GROUP` — signals work correctly |
| **Suspended Start** | `CREATE_SUSPENDED` — configure before resume |
| **Job Objects** | Auto-kill entire process tree on parent exit — no orphans |
| **Shells** | `pwsh` (PowerShell Core), `cmd`, `bash` (via Git for Windows/WSL) |

**No extra config needed.** If you're on Windows, the agent uses the Windows backend automatically.

---

## Subagent CLI Providers — Powered by Subprocess

When you use `subagent` with certain providers, the child runs as a managed subprocess:

| Provider | What It Runs | Subprocess Features Used |
|---|---|---|
| **Codex CLI** | `codex` CLI | `SubprocessStdio(collect)`, bounded output, env sanitization |
| **Claude Code** | `claude` CLI | Same |
| **ACP** | ACP-compatible agent | Stdio pipes, JSON-RPC over stdin/stdout |
| **DSH SDK** | Harness SDK subprocess | Full control channel (fd-7 protocol) |

**You don't configure this.** The agent picks the provider based on your request or availability.

---

## Safety & Isolation

| Protection | How |
|---|---|
| **No credential leakage** | Child gets reduced env — no server secrets, API keys |
| **Workspace-bound** | `cwd` restricted to conversation's workspace |
| **Tenant isolation** | Subprocess manager keys by `(tenant, user, conversation)` |
| **Output bounds** | 256 KiB max per session/job; spill to disk optional |
| **Process tree cleanup** | Windows Job Objects / Unix process groups — no orphans |
| **Sandbox fence** | Cannot change sandbox mode while terminals live |

---

## Limits (Admin Configurable)

| Setting | Default | Description |
|---|---|---|
| `deepspace_terminal_enabled` | `true` | Master switch |
| `deepspace_terminal_max_sessions_per_conversation` | `4` | Max live terminals per chat |
| `deepspace_terminal_max_sessions` | `32` | Deployment-wide cap |
| `deepspace_terminal_idle_timeout_seconds` | `900` (15 min) | Auto-close after silence |
| `deepspace_terminal_max_output_bytes` | `262144` (256 KiB) | Output buffer size |

---

## What "Working" Looks Like

1. **You ask for a terminal** → Agent calls `terminal_open` → **Side Panel → Terminals** shows new session
2. **Command runs** → Output streams into bounded buffer → You read via chat or panel
3. **Long build** → Agent writes with `background=true` → **Jobs panel shows the job id** → You poll with `terminal_job`
4. **Stuck command** → You say "Ctrl+C" → Agent sends `SIGINT` → Prompt returns
5. **Done** → You say "Close it" → Session ends, buffer released, process tree cleaned up

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| *"Shell unavailable"* | `bash`/`pwsh`/`cmd` not in PATH | Install shell, or check `deepspace_terminal_enabled=true` |
| *"Terminal limit reached"* | Too many live sessions | Close unused: `terminal_close` or Side Panel → ✕ |
| *"No such terminal session"* | Wrong `session_id` or already closed | Use `terminal_list` to see live ones |
| *Output truncated* | Buffer full (256 KiB) | Increase `deepspace_terminal_max_output_bytes` |
| *Session died unexpectedly* | Idle timeout hit | Increase `deepspace_terminal_idle_timeout_seconds` or send keep-alive |
| *Background job missing* | Wrong job id or the write has not started | Wait a few seconds, then `terminal_job` |
| *Windows: "ConPTY failed"* | `pywinpty` not installed / Windows version | Install `pywinpty`; requires Windows 10 1903+ |

---

## Technical Details (If You're Curious)

- **POSIX**: `pty.fork()` + `os.setsid()` → true pseudo-terminal, process group signals
- **Windows**: `pywinpty` + contained host subprocess → ConPTY/WinPTY, Job Objects
- **Input-wait detection**: Syscall tracing (`read`, `poll`, `epoll_wait`, etc.) — knows when child waits for stdin
- **Stdio dispositions**: `pipe` / `ignore` / `inherit` / `collect(max_bytes, spill?)` — unified across platforms
- **Output collection**: `SubprocessHandle` with async readers, bounded collection, optional spill
- **Tenant isolation**: Manager keys by `(tenant_id, user_id, conversation_id)`
- **Sandbox fence**: `SandboxModeFence` blocks `workspace-write` → `read-only` while sessions exist

---

## Related Documentation

| Topic | Document |
|---|---|
| Terminal API routes | `backend/docs/deepspace/11-deepspace-native-tool-surface.md` |
| Sandbox modes | `backend/docs/deepspace/22-configuration-reference.md` |
| Workspace paths | `backend/docs/deepspace/15-workspaces-and-files.md` |
| Subprocess stdio API | `backend/app/deepspace/services/subprocess_stdio.py` |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` (Terminals section) |

---

**TL;DR:** Ask for a terminal, a background job, or a subagent that uses Codex/Claude Code. The subprocess layer handles PTYs, Windows, signals, cleanup — you just say what you want.