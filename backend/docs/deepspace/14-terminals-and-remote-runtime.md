# 14. Terminals and the remote runtime

A terminal session is a shell that stays alive between tool calls, and the
remote runtime is the same idea on a machine the deployment has reviewed.
This chapter describes both: the local terminal sessions, their limits and
their ownership rules, and the remote runtime over SSH, its trust store, its
tools, and the boundary it keeps around the remote directory.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [The local terminal](#the-local-terminal)
  - [The terminal tools](#the-terminal-tools)
  - [The tenant rules](#the-tenant-rules)
  - [The idle sweep](#the-idle-sweep)
  - [The reviewed remote hosts](#the-reviewed-remote-hosts)
  - [The trust store](#the-trust-store)
  - [The remote tools](#the-remote-tools)
  - [The remote path boundary](#the-remote-path-boundary)
  - [Streaming a remote command](#streaming-a-remote-command)
  - [The remote terminal](#the-remote-terminal)
  - [The real-host proof](#the-real-host-proof)
- [What to watch for](#what-to-watch-for)

## Use case

- The agent starts a development server, then needs to read its output several
  turns later.
- A build takes five minutes, so the agent starts it in a session and does
  other work while it runs.
- A program asks a question and waits, so the agent has to answer it from
  another tool call.
- The code the agent works on lives on a build machine, not on the server that
  runs DeepSpace.

## Why it matters

- A shell command that starts and ends cannot hold state, so a server, a
  REPL, and an interactive prompt all have to be restarted on every turn.
- A terminal that is not bound to one tenant, one user, and one conversation
  lets one request read another request's output.
- A session that never ends holds a process and a buffer forever.
- A remote host that is reached without a reviewed host key can be replaced by
  another machine that answers for the same name.

## How it works

### The local terminal

`terminal_open` starts a child process in the conversation's workspace and
keeps it running. The shell kinds are `bash`, `pwsh`, and `cmd`.

On POSIX the child runs behind a real pseudo-terminal. On Windows DeepSpace
starts a helper through the shared subprocess launcher; the launcher assigns
that helper to a kill-on-close Job while suspended, and the helper then starts
the real PTY provided by the pinned `pywinpty` dependency. The shell inherits
the Job before it runs, and a framed stdio channel carries terminal output,
input, resize, and interrupt messages. The result of `terminal_open` carries
`pty: true` when the PTY starts successfully; Windows startup fails with a
typed shell-unavailable error if the PTY or Job Object containment cannot be
established.

The child never inherits the server's environment. It receives a fixed list
of variables built by `build_session_environment`:

| Variable | Value |
|---|---|
| `PATH` | The server's program search path. |
| `HOME` | The workspace root. |
| `TMPDIR` | `<workspace>/.deepspace-tmp`. |
| `LANG` | The server's locale, or `C.UTF-8`. |
| `TERM` | `xterm-256color`. |
| `DEEPSPACE_SHELL` | `1`. |
| `DEEPSPACE_SANDBOX` | `1`. |
| `DEEPSPACE_SANDBOX_MODE` | The containment mode in force. |
| `DEEPSPACE_SHELL_KIND` | The shell the session runs. |
| `DEEPSPACE_WORKSPACE` | The workspace root. |

A credential held in the server's environment is not readable from inside a
session.

### The terminal tools

| Tool | Mode | What it does |
|---|---|---|
| `terminal_open` | write | Opens a session and returns its identifier. |
| `terminal_read` | read | Reads the buffered output and removes it from the buffer. |
| `terminal_write` | write | Sends input to the session. |
| `terminal_signal` | write | Sends a signal to the session's foreground process group. |
| `terminal_close` | write | Closes the session and stops the process. |
| `terminal_list` | read | Lists the conversation's live sessions. |
| `terminal_job` | read | Reads one background terminal write. |

`terminal_write` settles with one reason, and the reason tells the caller
what the session is doing.

| Reason | What it means |
|---|---|
| `sent` | The input was delivered. |
| `stdin_read` | The session asked for more input. |
| `inferred_idle` | The session went quiet, so the command probably finished. |
| `timeout` | The wait ran out. |
| `session_exit` | The session's process is gone. |

The `stdin_read` reason is exact. The product reads the system call the
process is blocked in from the kernel's own record, so it knows the session
is waiting for input rather than guessing. The `inferred_idle` reason is a
guess based on silence, and the product says so in the name.

`terminal_write` takes a `background` flag. A background write returns a job
identifier instead of waiting, and `terminal_job` reads the outcome later.

The output of a session is held in a bounded buffer. The buffer keeps
262 144 bytes and drops the oldest bytes once it reaches that size. The cap
is the setting `deepspace_terminal_max_output_bytes`, so a deployment can
change it. A read returns the oldest pending bytes, 16 384 by default and
262 144 at most.

The limits on how many sessions may exist are three settings.

| Setting | Default | What it limits |
|---|---|---|
| `deepspace_terminal_max_sessions_per_conversation` | 4 | Live sessions one conversation may hold. |
| `deepspace_terminal_max_sessions` | 32 | Live sessions one deployment may hold. |
| `deepspace_terminal_idle_timeout_seconds` | 900 | How long a session may sit unused. |

`deepspace_terminal_enabled` controls persistent terminal access. When it is
false, DeepSpace omits terminal tools and the workspace API refuses new or
interactive session operations. The close route remains available so an
existing session can be shut down. This switch is separate from
`deepspace_shell_enabled`, which controls one-shot shell commands.

### The tenant rules

A session belongs to one tenant, one user, and one conversation. A terminal
opened by an agent tool is additionally scoped to the exact agent: a child
cannot read, write, signal, close, or list a sibling's terminal, and the root
agent cannot take over a child-owned session. The authenticated conversation
panel remains available to the user so they can inspect or close any session
in their conversation. A tool request outside its ownership scope is answered
as if the session does not exist, with "No such terminal session." This
prevents an agent from discovering another agent's terminal identifier or
output.

`deepspace_shell_enabled` turns the local shell tools off entirely. When it
is off, the shell tools answer "The local shell tools are disabled on this
deployment."

A conversation in `read-only` mode may not open, write to, or signal a
session, and the refusal says so.

### The idle sweep

A session that is not used for `deepspace_terminal_idle_timeout_seconds` is
closed. The sweep runs when the `terminal_open` tool opens a session and
when the `terminal_list` tool lists them, so a session that goes idle is not
held open by a quiet conversation.

The panel routes for terminals are on the chat router:
`GET` and `POST /api/v1/deepspace/chats/{conversation_id}/terminals` list and
open sessions, and `GET`, `POST`, and `DELETE` on
`/api/v1/deepspace/chats/{conversation_id}/terminals/{session_id}` read,
write, and close one session. A session limit returns the status code 429.

### The reviewed remote hosts

A remote host is a machine the tenant added and the deployment reviewed. The
routes are `GET` and `POST /api/v1/deepspace/ssh-hosts`, and `PATCH` and
`DELETE /api/v1/deepspace/ssh-hosts/{host_id}`.

| Field | Required | What it is |
|---|---|---|
| `name` | yes | The name the agent uses to reach the host. |
| `hostname` | yes | The host or address to connect to. |
| `username` | yes | The user the command runs as. |
| `known_hosts_entry` | yes | The host key the connection must present. |
| `port` | no | The port, 22 by default. |
| `key_secret_ref` | no | The name of the private key the deployment holds. |
| `remote_workspace` | no | The reviewed directory the command runs in. |
| `enabled` | no | Whether the host may be reached, true by default. |
| `timeout_seconds` | no | How long one command may run, 60 by default. |
| `metadata_json.runtime_config` | no | Versioned helper launch, workspace, RPC limits, and sandbox policy. |

A host without a reviewed host key is refused. A host without a reviewed
remote directory has no boundary a path can be confined to, so its files are
not reachable.

### The trust store

Two rules make the remote boundary safe.

The host key is checked. Each connection builds a `known_hosts` file that
holds nothing but the reviewed entry, and connects with
`StrictHostKeyChecking=yes` and `UserKnownHostsFile` pointing at that file.
A host that presents a different key is refused. The file lives in a
private directory that only the server can read, and it is removed when the
connection ends.

The key never leaves the server. The database holds the name of the key, not
the key itself. The deployment resolves the name inside
`deepspace_ssh_key_directory`, which defaults to the AverQel home
directory's `ssh` folder. A key file that other users can read is refused,
because a shared key is not a private key.

The connection also uses `BatchMode=yes`, so a host that would ask for a
password fails instead of waiting, and `IdentitiesOnly=yes`, so the agent's
own keys are not offered.

### The remote tools

| Tool | Mode | What it does |
|---|---|---|
| `ssh_run` | write | Runs a command on a reviewed host. |
| `ssh_host_list` | read | Lists the reviewed hosts the conversation may reach. |
| `ssh_stream_read` | read | Reads the output of a streaming remote command. |
| `ssh_stream_close` | write | Closes a streaming remote command. |
| `ssh_fs_read` | read | Reads a UTF-8 text file up to 1 MB, in verified byte ranges. |
| `ssh_fs_read_range` | read | Reads a binary-safe range up to 180 KB. |
| `ssh_fs_read_text` | read | Reads remote text in bounded chunks. |
| `ssh_fs_stat` | read | Returns `stat` or `lstat` metadata. |
| `ssh_fs_write` | write | Atomically writes UTF-8 text, up to 1 MB. |
| `ssh_fs_write_bytes` | write | Atomically writes base64-transported bytes, up to 1 MB. |
| `ssh_fs_list` | read | Lists a directory in pages of up to 1 000 entries. |
| `ssh_fs_delete` | write | Removes a regular file or symbolic link. |
| `ssh_fs_move` | write | Moves a path within the reviewed directory. |
| `ssh_fs_edit` | write | Replaces exact text using a supplied current-file SHA-256. |
| `ssh_fs_patch` | write | Applies non-overlapping line edits with a supplied current-file SHA-256. |
| `ssh_terminal_open` | write | Opens a persistent remote terminal. |
| `ssh_terminal_read` | read | Reads the output of a remote terminal. |
| `ssh_terminal_write` | write | Sends input to a remote terminal. |
| `ssh_terminal_signal` | write | Signals a remote terminal. |
| `ssh_terminal_close` | write | Closes a remote terminal. |

`deepspace_ssh_runtime_enabled` turns the whole remote runtime off. It is on
by default.

The remote command runs in the host's reviewed directory with a reduced
environment. Only `LANG`, `LC_ALL`, `TERM`, `PATH`, `HOME`, and `TZ` are
carried, together with the DeepSpace sandbox markers. The deployment's secrets
never reach the remote shell.

One command may be at most 8 000 characters, and one remote command may
produce at most 256 000 bytes of output.

### The remote filesystem protocol and boundary

The default helper is a small Python standard-library program sent over the
authenticated SSH channel. A host may instead select a pinned Node script or
executable in its versioned `metadata_json.runtime_config`. A custom bootstrap
must have an absolute path and lowercase SHA-256; the client verifies the
digest before launching it. The protocol negotiates a version and helper hash,
frames JSON requests, caps frame size and outstanding calls, sends heartbeats,
applies request deadlines, and releases the control connection when its lease
expires or the runtime is disposed.
The application also closes leased helper connections during orderly shutdown
so a remote process does not linger until its lease expires.

Both the application and helper confine paths to the reviewed root. Existing
symlinks are resolved and an escape is refused. Writes use a private temporary
file and atomic rename. Text edits and line patches compare the current file
hash before changing it; stale edits are refused. Directory listing is bounded
and paged. Range reads are binary-safe, and a full text read checks that the
file hash stays consistent across chunks.

Remote commands default to Bubblewrap. The sandbox mounts system paths
read-only, binds only the reviewed workspace writable, gives the command a
private `/tmp`, and disables networking unless the reviewed host policy turns
it on. If `bwrap` is unavailable, command execution fails closed with
`SANDBOX_UNAVAILABLE`; it does not silently run uncontained. An operator can
explicitly select `sandbox_mode="disabled"` in the host runtime configuration,
which restores legacy command execution under the SSH account's normal
permissions. The embedded helper requires remote `python3`; pinned Node or
executable launches must exist at their configured paths.

The host runtime contract also includes `workspace`, `request_timeout_ms`,
`max_frame_bytes`, `max_pending`, `lease_ms`, and `heartbeat_ms`. The heartbeat
must be no more than half the lease.

### Streaming a remote command

`ssh_run` takes a `stream` flag. A streamed command keeps running after the
call that started it returns, and the result carries a session identifier
that begins with `sshstream-`.

`ssh_stream_read` returns the output that arrived since the last read and
waits up to 30 seconds for more, 1 second when the caller names none.
`ssh_stream_close` stops the command on the host and ends the session.

The registry holds 16 live streaming sessions and sweeps the ones that have
been idle for 300 seconds.

### The remote terminal

A remote terminal is an ssh connection that keeps a pseudo-terminal open on
the host. The shell is the host's own, started with `exec "${SHELL:-/bin/sh}"`
in the reviewed remote directory. The session identifier begins with
`sshterm-`, so a remote identifier never names a local session.

A signal reaches a remote session the only way it can. An interrupt, a quit,
and a stop travel through the remote terminal's line discipline as the
control characters `0x03`, `0x1c`, and `0x1a`. A termination ends the
connection. The supported remote signals are `SIGINT`, `SIGTERM`, `SIGHUP`,
`SIGQUIT`, `SIGKILL`, and `SIGTSTP`.

A remote session belongs to the tenant, user, and conversation that opened
it, and it is closed when it goes idle.

### The real-host proof

The real-host integration test starts a test SSH server on loopback with
generated host and client keys, then calls the runtime and tool handlers used
by DeepSpace. It is separate from the fast mocked unit suite and requires the
OpenSSH client and test SSH server. Bubblewrap containment additionally needs
a remote host that supports user namespaces. A skipped or unrun integration
test does not establish remote-host readiness.

## What to watch for

- Focused unit tests exercise the RPC peer against a local stdlib helper; they
  do not verify an external host's Bubblewrap, host-key setup, or custom helper
  launch. Run the isolated real-host integration test on a host with the
  required SSH and Bubblewrap support before making a production-readiness
  claim.
- A local terminal session lives in the server process, so it disappears when
  the server restarts. There is no durable record of a session.
- The local terminal does not run inside the operating-system cage. The
  session is confined by the workspace path rules and the reduced
  environment, not by a namespace.
- Windows PTY sessions use pywinpty and a kill-on-close Job Object. The PTY
  process is assigned immediately after the library starts it; because the
  library does not expose suspended creation, there is a short startup window
  before assignment. Descendants started after assignment are covered by the
  Job. The Windows-specific path still needs execution on a native Windows
  runner before release.
- The `inferred_idle` reason is a guess. A command that pauses without
  producing output is reported as probably finished.
- A read removes the bytes it returns, so a second read of the same session
  does not repeat them.
- The idle sweep runs when the agent opens or lists a session. A conversation
  that never opens and never lists one leaves its sessions alive until the
  process restarts.
- The local session limits count sessions, not processes. A session that
  started several children still counts once.
- The remote path boundary covers the reviewed remote directory only. A
  command run with `ssh_run` is not confined to a path, because a command
  can reach anything the user on the host can reach.
- A host that presents a different key is refused, and the operator has to
  review the new key and add it before the host is reachable again.
- A private key that other users can read is refused. The key directory has
  to be private on the server.
- The remote terminal's signals travel as control characters, so a program
  that does not read them as signals may not react to them.
