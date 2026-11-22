# DeepSpace Shell & Terminal — User Guide

**Date:** 2026-11-14

---

## What It Is

**Shell tools** run commands in your workspace. **Terminal tools** give you persistent PTY sessions that survive across chat turns.

**Use shell tools for:** one-off commands (build, test, git, ls)
**Use terminal tools for:** long-running processes, REPLs, interactive prompts, background jobs

---

## What You See in the UI

| Location | What It Shows |
|---|---|
| **Chat** | Agent runs commands, shows output, handles errors |
| **Side Panel → Terminals** | Live sessions, output, signals, close button |
| **Jobs panel** | Background terminal writes with job ids |

---

## How to Use It (Natural Language)

| You Say | Agent Does |
|---|---|
| *"Run `npm test`"* | `bash` tool (one-shot) |
| *"Run `Get-Process` in PowerShell"* | `pwsh` tool (one-shot) |
| *"Open a terminal and run `npm run dev`"* | `terminal_open(shell="bash", command="npm run dev")` |
| *"What's the output?"* | `terminal_read(session_id="...")` |
| *"Send Ctrl+C"* | `terminal_signal(session_id="...", signal="SIGINT")` |
| *"Run the build in background"* | `terminal_write(session_id="...", data="./build.sh\n", background=true)` → returns `job_id` |
| *"How's the build?"* | `terminal_job(job_id="...")` |
| *"Close that terminal"* | `terminal_close(session_id="...")` |
| *"List my terminals"* | `terminal_list()` |

---

## One-Shot Commands (Shell Tools)

| Tool | Shell | Use For |
|---|---|---|
| `bash` | bash | Linux/macOS commands, builds, git, scripts |
| `pwsh` | PowerShell Core | Windows commands, .NET, cross-platform |

**Both respect your permission mode:**
- `read-only` — refuses mutating commands (`rm`, `docker`, `npm install`, etc.)
- `workspace-write` — runs in workspace, refuses paths outside it
- `danger-full-access` — no restrictions (admin only)

---

## Persistent Terminals (Terminal Tools)

| Feature | Details |
|---|---|
| **Survives turns** | Open once, use all conversation |
| **True PTY** | Programs think they're talking to a human |
| **Shells** | `bash` (default), `pwsh`, `cmd` |
| **Bounded output** | Last 256 KiB kept (configurable) |
| **Idle timeout** | Auto-closes after 15 min silence (configurable) |
| **Signals** | `SIGINT` (Ctrl+C), `SIGTERM`, `SIGHUP`, `SIGKILL`, `SIGTSTP`/`SIGCONT` |
| **Background writes** | `terminal_write(..., background=true)` returns a job id; read it with `terminal_job` |
| **Per-conversation isolation** | Your terminals are yours only |

---

## Background Writes

```
User: "Run the test suite in background"
Agent: terminal_write(session_id="...", data="pytest\n", background=true) → job_id: "369afb21..."

... later ...

User: "How's the test?"
Agent: terminal_job(job_id="369afb21...") → reports status and wait_reason
       terminal_read(session_id="...") → shows the collected output
```

**Key points:**
- The terminal belongs to the conversation; close it when the work is done.
- Output is bounded at 256 KiB; older bytes drop off.
- Poll the write with `terminal_job`, and read the output with `terminal_read`.
- Kill a running command with `terminal_signal` on its session.

---

## Windows / macOS / Linux — It Just Works

| Platform | Sandbox Backend | What You Get |
|---|---|---|
| **Linux** | `bwrap` (bubblewrap) preferred, `landlock` fallback | Full filesystem + network isolation |
| **macOS** | `sandbox-exec` (seatbelt) | Full filesystem + network isolation |
| **Windows** | `windows-acl` (restricted token + Job Objects) | Write/delete confinement, auto-cleanup |
| **No sandbox** | `none` (if `deepspace_sandbox_required=false`) | Inspection only, runs anyway |

**Auto-selected.** No config needed.

---

## Safety & Isolation

| Protection | How |
|---|---|
| **No credential leakage** | Child gets reduced env — no server secrets |
| **Workspace-bound** | `cwd` restricted to conversation's workspace |
| **Tenant isolation** | Manager keys by `(tenant, user, conversation)` |
| **Output bounds** | 256 KiB max per session/job; spill to disk automatic |
| **Process tree cleanup** | Windows Job Objects / Unix process groups — no orphans |
| **Sandbox fence** | Cannot change sandbox mode while terminals live |

---

## Limits (Admin Configurable)

| Setting | Default | Description |
|---|---|---|
| `deepspace_shell_enabled` | `true` | Master switch for shell tools |
| `deepspace_terminal_enabled` | `true` | Master switch for terminal tools |
| `deepspace_terminal_max_sessions_per_conversation` | `4` | Max live terminals per chat |
| `deepspace_terminal_max_sessions` | `32` | Deployment-wide cap |
| `deepspace_terminal_idle_timeout_seconds` | `900` (15 min) | Auto-close after silence |
| `deepspace_terminal_max_output_bytes` | `262144` (256 KiB) | Output buffer size |
| `deepspace_shell_capture_bytes` | `8000000` (8 MB) | One-shot command capture before spill |
| `deepspace_shell_timeout_seconds` | `120` | One-shot command timeout |

---

## Spill on Large Output

If a command outputs more than 24K chars, DeepSpace **automatically spills** the full output to storage and shows a notice:

```
⋯ 1,234,567 bytes spilled (use spill_read to retrieve) ⋯
```

Agent can call `spill_read` to get the full output. Nothing is lost.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| *"Shell unavailable"* | `bash`/`pwsh`/`cmd` not in PATH | Install shell, or check `deepspace_shell_enabled=true` |
| *"Terminal limit reached"* | Too many live sessions | Close unused: `terminal_close` or Side Panel → ✕ |
| *"No such terminal session"* | Wrong `session_id` or already closed | Use `terminal_list` to see live ones |
| *Output truncated* | Buffer full (256 KiB) | Increase `deepspace_terminal_max_output_bytes` |
| *Session died unexpectedly* | Idle timeout hit | Increase `deepspace_terminal_idle_timeout_seconds` or send keep-alive |
| *Background job missing* | Job id is wrong or the write is still starting | Wait a few seconds, then `terminal_job` |
| *"Sandbox unavailable"* | No bubblewrap/sandbox-exec/Windows ACL | Install backend, or set `deepspace_sandbox_required=false` |
| *"Permission denied"* | Command refused by mode | Switch to `workspace-write` or `danger-full-access` |

---

## Technical Details (If You're Curious)

- **POSIX**: `pty.fork()` + `os.setsid()` → true pseudo-terminal, process group signals
- **Windows**: `pywinpty` + contained host subprocess → ConPTY/WinPTY, Job Objects
- **Input-wait detection**: Syscall tracing (`read`, `poll`, `epoll_wait`, etc.)
- **Stdio dispositions**: `pipe` / `ignore` / `inherit` / `collect(max_bytes, spill?)`
- **Tenant isolation**: Manager keys by `(tenant_id, user_id, conversation_id)`
- **Sandbox fence**: `SandboxModeFence` blocks `workspace-write` → `read-only` while sessions exist

---

## Related Documentation

| Topic | Document |
|---|---|
| Shell API routes | `backend/docs/deepspace/11-deepspace-native-tool-surface.md` |
| Sandbox modes | `backend/docs/deepspace/22-configuration-reference.md` |
| Workspace paths | `backend/docs/deepspace/15-workspaces-and-files.md` |
| Subprocess stdio API | `backend/app/deepspace/services/subprocess_stdio.py` |
| In-app help | `frontend/app/documentation/deepspace/page.tsx` (Terminals section) |

---

**TL;DR:** Ask for a command → agent runs `bash`/`pwsh`. Ask for a terminal → agent opens persistent PTY. Ask for background job → agent returns handle, you poll later. Sandbox handles isolation automatically.