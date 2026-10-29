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

On POSIX the child runs behind a real pseudo-terminal, so a program that
checks whether it has a terminal behaves the way it does for a person. On
Windows the standard library offers no pseudo-terminal, so the session runs
over ordinary pipes and reports `pty: false`. The result of `terminal_open`
carries the `pty` flag.

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

### The tenant rules

A session belongs to one tenant, one user, and one conversation. A request
for a session that belongs to somebody else is answered exactly as a request
for a session that does not exist, with the message "No such terminal
session." A caller therefore cannot learn that another tenant's session
exists.

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
| `ssh_fs_read` | read | Reads a text file on a reviewed host. |
| `ssh_fs_write` | write | Writes a text file on a reviewed host. |
| `ssh_fs_list` | read | Lists a directory on a reviewed host. |
| `ssh_fs_delete` | write | Removes a file on a reviewed host. |
| `ssh_terminal_open` | write | Opens a persistent remote terminal. |
| `ssh_terminal_read` | read | Reads the output of a remote terminal. |
| `ssh_terminal_write` | write | Sends input to a remote terminal. |
| `ssh_terminal_signal` | write | Signals a remote terminal. |
| `ssh_terminal_close` | write | Closes a remote terminal. |

`deepspace_ssh_runtime_enabled` turns the whole remote runtime off. It is on
by default.

The remote command runs in the host's reviewed directory, and the client
process receives a reduced environment. Only `LANG`, `LC_ALL`, `TERM`,
`PATH`, `HOME`, and `TZ` are carried, together with `DEEPSPACE_SANDBOX=1` and
`DEEPSPACE_SANDBOX_BACKEND=ssh`. The deployment's secrets never reach the
remote shell.

One command may be at most 8 000 characters, and one remote command may
produce at most 256 000 bytes of output.

### The remote path boundary

Every remote file path is resolved on the host, inside the reviewed remote
directory. The product sends a small POSIX shell prelude that walks the
requested path one segment at a time, follows every link it meets, and
prints the physical path it reached. The path is accepted only when that
physical path is the reviewed directory or lies under it.

The resolution happens on the host because only the host can see the links.
A link inside the directory that points outside it is followed and refused on
the host.

The prelude reports what it found through exit codes.

| Exit code | Meaning |
|---|---|
| 4 | The path leaves the reviewed directory. |
| 5 | The path does not exist. |
| 6 | The path is not the kind the call needs. |
| 7 | The reviewed directory could not be reached. |

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

The remote runtime is proved against a real SSH server. The proof starts a
server on the loopback interface with a generated host key, a generated
client key, and an `authorized_keys` file, and points the runtime at it:
the same trust store, the same settings, and the same tools the model calls.

The proof covers a file read, a file write, a directory listing, a path that
leaves the reviewed directory, a remote terminal, and a streamed command. The
read and the listing answer with what is on the host, the write lands in the
reviewed directory, the refused path is refused both when it names a parent
directory and when it is a link out of the directory, the terminal keeps a
shell open on the host, and the streamed command reports its output while it
runs.

The proof needs the OpenSSH client, because that is what the runtime runs
every command through, and the test-only SSH server the proof starts. It
skips when either one is missing.

## What to watch for

- The remote runtime is proved against a real SSH server, on the loopback
  interface, with the product's own tools. The proof skips on a host without
  the OpenSSH client, because that is what the runtime runs every command
  through, and on a host without the test-only SSH server it starts.
- A local terminal session lives in the server process, so it disappears when
  the server restarts. There is no durable record of a session.
- The local terminal does not run inside the operating-system cage. The
  session is confined by the workspace path rules and the reduced
  environment, not by a namespace.
- On Windows a session runs over pipes and reports `pty: false`, so a program
  that checks for a terminal behaves differently there.
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
