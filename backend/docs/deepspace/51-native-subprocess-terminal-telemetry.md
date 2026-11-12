# 51. Native subprocess containment, terminal readiness, and telemetry channels

This chapter documents the capabilities DeepSpace gained from the DeepSeek
Harness comparison: native OS process containment, terminal readiness
protocol, shell activity tracking, sandbox mode fence, pwsh preamble, and
independent telemetry channels.

## Native OS process containment

`app/deepspace/services/native_containment.py` currently provides an
opt-in containment API, not a containment guarantee for every DeepSpace child:

- `LinuxNativeRuntime` — uses `systemd-run --scope --user` to create a
  private systemd scope owning the entire descendant tree. Falls back to
  `unshare -U` private bootstrap when systemd is unavailable. A
  `LinuxScopeSelector` probes and caches availability, re-probing only after
  invalidation, and warns on fallback.
- `WindowsJobRuntime` — shared-launcher children start with
  `CREATE_SUSPENDED`, enter a kill-on-close Job Object, and resume only after
  assignment succeeds. If the Job cannot be created or assigned, the child is
  killed and the launch fails closed. This path has mocked Windows API tests
  and still needs a native Windows run.
- `NativeContainmentManager` — platform-aware manager exposing
  `create_range()`, `track_process_group()`, and `register_host_exit()` for
  synchronous finalization of registered ranges on host exit. POSIX terminal,
  shared stdio, Python/Node PTC, hook, and shell launches register their
  isolated process groups and release those registrations after reaping.
- `ManagedRange` — tracks one managed range with `signal()`,
  `terminate_for_host_exit()`, and `mark_stopped()`.

Host-exit finalization is registered at API startup via `atexit`. POSIX
process-group tracking provides lifecycle cleanup only; it does not impose
resource limits or replace the feature-specific sandbox. The Windows terminal
starts a small `windows_pty_host` process through the shared launcher. The
launcher assigns the host while suspended; pywinpty then starts the real PTY
shell as a descendant of that job member, so the shell inherits the Job before
its code runs. The host carries terminal input, output, resize, and interrupt
messages over framed stdio. Startup and Job assignment failures close the
session instead of opening an uncontained terminal.

## Process inspection

`app/deepspace/services/process_inspector.py` provides /proc-based (Linux)
and ps-based (macOS) process table inspection: `foreground_pgid`,
`is_stdin_waiting`, `snapshot`, `is_alive`, `signal_group`,
`signal_process`, `tree`, `session_members`. A `create_process_inspector()`
factory selects the platform implementation.

## Output collection with spill and offset readers

`app/deepspace/services/output_collector.py` provides `OutputCollector` with
bounded in-memory tail, optional spill file, and `read_from()` for
offset-based incremental reads. `app/deepspace/services/subprocess_stdio.py`
provides `SubprocessStdio`, executable resolution against the child `PATH`, a
credential/DeepSpace-state scrub for inherited environment variables, shared
proxy policy (with an explicit opt-out for the hook allowlist), POSIX
process-group or Windows Job termination with a grace period, drain-aware
output collection, and host-exit tracking. The ACP, DSH SDK, Claude Code, and
Codex subagent providers, embedded browser, Playwright browser server, SSH,
shell, Python/Node PTC, hooks, LSP, workspace diff, VNC, and computer-use
command consumers use this launcher. Playwright starts its browser server
under this process owner; the server and every browser descendant inherit the
same group or Windows Job. The Python client connects to a random, loopback-only
Playwright WebSocket endpoint. Proxy credentials are sent to the server over
its private stdin pipe, never in command-line arguments.
Explicit provider
environment entries are opt-ins and are applied after the inherited scrub. A
background reaper releases each registration after the child exits, including
when a caller reads the exposed streams directly. Persistent plugin JSON-RPC
bridges, sandbox runners, native-command openers, and pywinpty keep specialized
lifecycle owners for their protocols or operating-system APIs.

The `control='pipe'` setting currently fails fast with `NotImplementedError`;
the DSH-compatible fd-7 duplex transport has not been implemented. The helper
also does not replace the plugin JSON-RPC bridge's persistent synchronous
process owner or the OS-specific sandbox and native-command launchers.

## Shell activity tracking

`app/deepspace/services/shell_activity.py` provides `ShellActivity` with
`invalidate()`, `inspect(pid)`, and `dispose()`, plus `prepare_shell_activity()`
for bash/zsh integration with controlled prompt markers.

## Terminal readiness protocol

`TerminalSession` gained `inspect_activity()` returning `{state, revision}`
where state is `idle`/`busy`/`unknown`. The controlled prompt marker
(OSC 133;D;) is detected by `TerminalSanitizer` in
`app/deepspace/services/terminal_sanitizer.py`.

## Sandbox mode fence

`app/deepspace/services/sandbox_mode_fence.py` provides `SandboxModeFence`
which blocks sandbox mode changes while terminal sessions are live.
`SandboxModeService` now accepts an optional `terminal_manager` and enforces
the fence in `set_mode()`.

## pwsh preamble

`app/deepspace/services/pwsh_preamble.py` provides `ENCODING_PREAMBLE`,
`PWSH_PROMPT_SETUP`, `BASH_PROMPT_SETUP`, and helpers for reliable pwsh
startup with UTF-8 encoding and controlled prompt.

## Pluggable terminal backends

`app/deepspace/services/terminal_backend.py` provides `TerminalBackend` ABC,
`TerminalBackendSession`, `TerminalBackendSpawnSpec`, and
`TerminalBackendRegistry`. `TerminalManager` dispatches every terminal launch
through its registry; a native backend is registered by default, and trusted
runtime composition can inject or register another backend. The manager still
performs workspace resolution, sandbox enforcement, and ownership checks
before dispatch. Initial terminal rows and columns are validated and passed
through to the backend; POSIX applies them to its PTY before shell startup,
while Windows uses pywinpty for a real PTY and a kill-on-close Job. The Windows
path has mocked API tests and still needs execution on a native Windows runner.

## Independent telemetry channels

`app/deepspace/services/telemetry_events.py` provides:

- `EventLogReporter` — fire-and-forget `emit(record)` with count-based
  batching and per-consumer ownership. Queue and batch sizes are validated;
  full queues reject new records instead of growing without bound.
- `SessionLogReporter` — byte-bounded (4MB) queue with count+byte limits,
  single in-flight transport, `stop_pending()`, and `shutdown()`. Oversized
  records are rejected without truncation; `shutdown()` stops new records and
  flushes queued work, while `stop_pending()` intentionally discards it.
- `TelemetryChannelFactory` — creates independent channels.

`SessionTelemetry` owns both reporters when `deepspace_telemetry_enabled` and
`deepspace_telemetry_logs_endpoint` are configured. The application binds the
reporters to its event loop; typed session events are exported only after the
database transaction commits, and rollback discards their queued summaries.
Exports contain event type, sequence, turn, step, and tool name, not encrypted
payloads, prompts, or tool results. Shutdown flushes both queues.

## Verification snapshot (2026-11-12)

The focused isolated run passed **155 tests**. Branch coverage was **100%**
for `durable_subagent_manager.py`, `subprocess_stdio.py`,
`windows_pty_host.py`, and Typert's `resource_authorization.py` (533 statements
and 166 branches). The Windows Job and PTY behavior was exercised through
mocked Windows APIs in this Linux run; it still needs a native Windows run.
