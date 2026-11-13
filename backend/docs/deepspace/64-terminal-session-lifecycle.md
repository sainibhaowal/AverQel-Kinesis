# 64. Terminal Session Lifecycle and Sanitize Rules

The terminal tools (`terminal_open`, `terminal_read`, `terminal_write`,
`terminal_close`, `terminal_list`, `terminal_signal`, `terminal_job`) manage
persistent shell sessions with PTY-based I/O, output sanitization, and
job-control integration.

## Use case

- A user needs a long-running shell session that survives across turns.
- A workflow runs a background command and streams its output.
- The model interacts with a REPL or interactive CLI.

## Why it matters

- PTY sessions provide true terminal semantics (colors, cursor, SIGWINCH).
- Sanitize rules prevent control-sequence injection from untrusted command output.
- Job control (`terminal_signal`, `terminal_job`) enables graceful interruption.

## How it works

### Session lifecycle (`terminal.py`)

```
terminal_open
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│  TerminalSession                                                │
│  - pty: PtyProcess (os.openpty / winpty)                       │
│  - buffer: RingBuffer (last N lines + ANSI state)              │
│  - cwd: str                                                     │
│  - env: dict                                                    │
│  - signal_handler: SIGINT/SIGTERM forwarding                   │
└─────────────────────────────────────────────────────────────────┘
    │
    ├── terminal_read  ──► returns sanitized text + cursor pos
    ├── terminal_write ──► writes to PTY stdin
    ├── terminal_signal ──► sends SIGINT/SIGTERM/SIGKILL
    ├── terminal_job   ──► runs command, streams output, returns exit code
    ├── terminal_list  ──► lists all sessions for conversation
    └── terminal_close ──► terminates PTY, releases resources
```

### PTY backend selection

| Platform | Backend | Notes |
|---|---|---|
| Linux | `os.openpty` + `fcntl` | Full PTY semantics |
| macOS | `os.openpty` | Full PTY semantics |
| Windows | `winpty` (bundled) | ConPTY API on Win10+, winpty fallback |

### Sanitize rules (`terminal.py:sanitize_output`)

All PTY output passes through `sanitize_output()` before being returned to
the model:

1. **Strip OSC sequences** (Operating System Commands):
   - `ESC ] ... BEL` and `ESC ] ... ST` — window title, clipboard, etc.
   - Exception: OSC 8 (hyperlinks) preserved as plain text.

2. **Strip CSI sequences** (Control Sequence Introducer) except:
   - `CSI n A/B/C/D/E/F` — cursor movement (cursor position tracked)
   - `CSI n J/K` — erase display/line
   - `CSI n m` — SGR (colors, bold, etc.) **preserved for rendering**
   - `CSI ? h/l` — DEC private modes (bracketed paste, etc.)

3. **Strip DCS/APC/PM/SOS** — device control, application program command, etc.

4. **Normalize line endings** → `\n`.

5. **Truncate** to `terminal_max_output_chars` (default 50,000) per read.

6. **Track cursor position** for `terminal_read` response:
   ```python
   {
       "text": "sanitized output...",
       "cursor": {"row": 10, "col": 5},
       "truncated": false
   }
   ```

### Terminal job (`terminal_job`)

Runs a command to completion with streaming output:

```json
{
  "name": "terminal_job",
  "arguments": {
    "session_id": "uuid",
    "command": "pytest tests/ -v",
    "timeout_seconds": 300,
    "stream": true
  }
}
```

- Creates a temporary session or reuses existing.
- Streams sanitized output via `terminal_read` events.
- Returns `{ "exit_code": 0, "output": "..." }` on completion.
- Timeout sends `SIGTERM` then `SIGKILL`.

### Terminal signal (`terminal_signal`)

```json
{
  "name": "terminal_signal",
  "arguments": {
    "session_id": "uuid",
    "signal": "SIGINT"  // or SIGTERM, SIGKILL
  }
}
```

- `SIGINT` (Ctrl+C) — interrupts foreground process.
- `SIGTERM` — graceful shutdown request.
- `SIGKILL` — immediate termination (no cleanup).

### Session listing (`terminal_list`)

Returns all sessions for the current conversation:
```json
{
  "sessions": [
    {
      "id": "uuid",
      "cwd": "/workspace",
      "command": "bash",
      "started_at": "2026-01-01T00:00:00Z",
      "alive": true,
      "cols": 80,
      "rows": 24
    }
  ]
}
```

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_terminal_max_sessions` | `int` (1-64) | `16` | Max concurrent sessions per conversation. |
| `deepspace_terminal_max_output_chars` | `int` (1000-1000000) | `50000` | Per-read output ceiling. |
| `deepspace_terminal_sanitize` | `bool` | `true` | Enable sanitize rules. |
| `deepspace_terminal_shell` | `str` | `"bash"` | Default shell for new sessions. |

## What to watch for

- **Sanitization is not a security boundary** — it prevents rendering glitches
  and cursor chaos.  The model still receives sanitized text; malicious output
  cannot execute code.
- **Windows winpty** adds ~50ms latency per write; batch writes when possible.
- **Buffer ring** keeps last 1000 lines by default; older output is dropped.
- **SIGWINCH** is sent automatically on `terminal_read` with explicit `cols`/`rows`
  if the model requests resize.
- **Job streaming** uses the same sanitize pipeline; ANSI colors from tools
  (e.g., `pytest --color=yes`) are preserved via SGR pass-through.

## Related chapters

- [Terminals and the Remote Runtime](14-terminals-and-remote-runtime.md) — SSH terminal integration.
- [Native Subprocess, Terminal, Telemetry](51-native-subprocess-terminal-telemetry.md) — shared subprocess infrastructure.
