# DeepSpace Terminal — User Guide

**Date:** 2026-11-14

---

## What It Is

A **persistent terminal** is a long-lived shell session that survives across chat turns. Unlike a one-off command, a terminal keeps its state — working directory, environment variables, running processes, REPLs — so you can:

- Run a build that takes minutes and check its output later
- Keep a Python/Node REPL open and send it code step by step
- Start a background job, do other work, then come back for the result
- Interrupt a hanging command with Ctrl+C (or other signals)

Each terminal belongs to **your conversation only** — no other user or tenant can see or touch it.

---

## What You See in the UI

| Location | What It Does |
|---|---|
| **Side Panel → Terminals tab** | Lists your live sessions, lets you open/close, shows recent output |
| **Chat** | Agent can open, write to, read from, signal, and close terminals for you |
| **Jobs panel** | Background terminal writes appear here with a job id you can read with `terminal_job` |

---

## How to Use It (Natural Language)

Just ask the agent. Examples:

| You Say | Agent Does |
|---|---|
| *"Open a terminal and run `npm run build`"* | `terminal_open(shell="bash", command="npm run build")` |
| *"What's the output of that terminal?"* | `terminal_read(session_id="...")` |
| *"Send Ctrl+C to stop it"* | `terminal_signal(session_id="...", signal="SIGINT")` |
| *"Run this long script in the background"* | `terminal_write(session_id="...", data="./build.sh\n", background=true)` → returns `job_id` |
| *"Check the background job"* | `terminal_job(job_id="...")` |
| *"Close that terminal"* | `terminal_close(session_id="...")` |
| *"List my terminals"* | `terminal_list()` |

You never need to know the tool names — the agent picks the right one.

---

## Key Capabilities

### Persistent Sessions
- **Survives across turns** — open once, use all conversation
- **Bounded output** — keeps last 256 KiB (configurable), older output drops off
- **Idle timeout** — auto-closes after 15 minutes of silence (configurable)
- **Per-conversation limit** — max 4 simultaneous sessions by default

### Shell Choice
- `bash` (default), `pwsh` (PowerShell Core), `cmd` (Windows)
- Agent picks based on your OS, or you can specify: *"Open a PowerShell terminal"*

### Background Writes
- `terminal_write(session_id, data, background=true)` returns a `job_id` immediately
- Read the job later with `terminal_job(job_id="...")` — it reports status and `wait_reason`
- Read the session output with `terminal_read(session_id="...")`; the write itself only reports whether the input was delivered

### Signals
| Signal | Use For |
|---|---|
| `SIGINT` (Ctrl+C) | Interrupt a running command |
| `SIGTERM` | Graceful shutdown request |
| `SIGHUP` | Hangup (often reloads config) |
| `SIGKILL` | Force kill (last resort) |
| `SIGTSTP` / `SIGCONT` | Suspend / resume (Ctrl+Z / `fg`) |

### Safety
- **No credential leakage** — child gets a reduced environment (no server secrets)
- **Workspace-bound** — `cwd` stays inside your conversation's workspace
- **Tenant isolation** — you cannot see or touch another tenant's sessions
- **Sandbox fence** — you cannot change sandbox mode while terminals are live

---

## Configuration (Admin/Deployment)

| Setting | Default | Description |
|---|---|---|
| `deepspace_terminal_enabled` | `true` | Master switch |
| `deepspace_terminal_max_sessions_per_conversation` | `4` | Max live sessions per chat |
| `deepspace_terminal_max_sessions` | `32` | Deployment-wide cap |
| `deepspace_terminal_idle_timeout_seconds` | `900` (15 min) | Auto-close after silence |
| `deepspace_terminal_max_output_bytes` | `262144` (256 KiB) | Output buffer size |

---

## What "Working" Looks Like

1. **You ask** → Agent opens terminal → **Side Panel shows new session** with `session_id`
2. **Command runs** → Output streams into buffer → **You read it** via chat or panel
3. **Long build** → Agent writes with `background=true` → **Jobs panel shows the job id** → You poll with `terminal_job`
4. **Stuck command** → You say "Ctrl+C" → Agent sends `SIGINT` → Command stops, prompt returns
5. **Done** → You say "Close it" → Session ends, buffer released

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| *"Shell unavailable"* | `bash`/`pwsh`/`cmd` not in PATH | Install the shell, or ensure `deepspace_terminal_enabled=true` |
| *"Terminal limit reached"* | Too many live sessions | Close unused ones: `terminal_close` or Side Panel → ✕ |
| *"No such terminal session"* | Wrong `session_id` or already closed | Use `terminal_list` to see live ones |
| *Output truncated* | Buffer full (256 KiB) | Increase `deepspace_terminal_max_output_bytes` |
| *Session died unexpectedly* | Idle timeout hit | Increase `deepspace_terminal_idle_timeout_seconds` or send keep-alive |
| *Background job missing* | Worker hasn't picked it up yet | Wait a few seconds, then `terminal_job` |

---

## Technical Details (If You're Curious)

- **POSIX**: `pty.fork()` + `os.setsid()` → true pseudo-terminal
- **Windows**: `pywinpty` + contained host subprocess → ConPTY/WinPTY
- **Input-wait detection**: Syscall tracing (`read`, `poll`, `epoll_wait`, etc.) — knows when child waits for stdin
- **Tenant isolation**: Manager keys sessions by `(tenant_id, user_id, conversation_id)`
- **Sandbox fence**: `SandboxModeFence` blocks `workspace-write` → `read-only` while sessions exist

---

## Related Documentation

| Topic | Document |
|---|---|
| Terminal API routes | `backend/docs/deepspace/11-deepspace-native-tool-surface.md` |
| Sandbox modes | `backend/docs/deepspace/22-configuration-reference.md` |
| Workspace paths | `backend/docs/deepspace/15-workspaces-and-files.md` |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` (Terminals section) |

---

**That's it.** Open a terminal, run things, come back later. The agent handles the tool calls; you just say what you want.