# 13. The sandbox

The sandbox is the operating-system boundary a command runs inside. DeepSpace
runs real shell commands, and this chapter describes the boundary those
commands run under: which backends the product can use, how the user chooses
how much of the host a command may reach, and what a deployment does when the
host cannot offer a boundary at all.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [The backends](#the-backends)
  - [The permission mode](#the-permission-mode)
  - [The approval mode](#the-approval-mode)
  - [The network switch](#the-network-switch)
  - [The read-only inspection](#the-read-only-inspection)
  - [Deadlines](#deadlines)
  - [The routes that report the cage](#the-routes-that-report-the-cage)
- [What to watch for](#what-to-watch-for)

## Use case

- An operator runs DeepSpace on a machine that holds other people's data, so a
  command the agent writes must not reach beyond the workspace.
- A developer wants the agent to install a package, so the command needs the
  network while it runs and must lose it afterwards.
- A team wants one conversation to be read-only while another conversation in
  the same deployment edits files.
- A security reviewer asks what actually stops a command, and the answer has
  to name a mechanism rather than a promise.

## Why it matters

- A shell command runs with the server's own rights, so a command that escapes
  the workspace reaches everything the server can reach.
- A permission mode that only the deployment can set makes every conversation
  alike, and one careless conversation changes the rules for the rest.
- A missing cage that silently falls back to no cage is worse than a refusal,
  because the operator believes the command was contained.
- A broken cage and a refused command look the same to a reader, and the two
  need different repairs.

## How it works

Two layers bound a command. The first reads the command line and refuses one
that plainly breaks the rules. The second runs the command inside an
operating-system boundary so that a rule the reader missed is still enforced
by the kernel.

### The backends

`deepspace_sandbox_backend` names the backend. The default is `auto`, which
probes the host and takes the first backend that works.

| Value | Platform | What it confines | Enforcement |
|---|---|---|---|
| `auto` | any | Picks the best backend the host offers. | Depends on the pick. |
| `bwrap` | Linux | Its own mount, process, IPC, UTS, and cgroup namespaces. | `full` |
| `landlock` | Linux | File effects by path. | `partial` |
| `sandbox-exec` | macOS | Reads, writes, and the network, through a seatbelt profile. | `full` |
| `windows` | Windows | Writes and deletes, through a restricted token. | `partial` |
| `docker` | any | A container, with the workspace mounted. | `full` |
| `none` | any | Nothing. The command runs as the server does. | `none` |

The `bwrap` backend is the strongest one on Linux. It gives the command its
own mount, process, IPC, UTS, and cgroup namespaces, mounts a fresh `/tmp`,
`/home`, and `/root`, exposes the system directories read-only, and makes the
workspace the only writable path. It sets `HOME` to the workspace and
`TMPDIR` to `/tmp`.

The `landlock` backend is for a host where unprivileged user namespaces are
unavailable and bubblewrap therefore cannot start. It confines file effects
only. It does not restrict the network.

The `sandbox-exec` backend writes a seatbelt profile that denies everything by
default and then admits the system directories, the workspace, and process
calls. The network is admitted or denied by the deployment's network setting.

The `windows` backend runs the command with a restricted token whose
restricting security identifiers carry a capability that only the workspace
grants, at a low integrity level. A write elsewhere then fails even when the
user could normally write there. The runner fails closed: if any step of the
confinement does not succeed, the command never starts.

The `docker` backend runs the command in a container with the workspace
mounted. It adds `--network none` when the network is off, mounts the
workspace read-only in read-only mode, and runs as the server's own user id
on POSIX systems. It is skipped unless `deepspace_sandbox_image` names an
image, so no image is pulled without an explicit choice.

`deepspace_sandbox_required` decides what happens when no backend is
available. It is off by default, so an unsupported host still works. When it
is on, a command is refused instead of running uncontained, and the refusal
carries the stable code `SANDBOX_UNAVAILABLE` together with the runners the
platform offers.

### The permission mode

The permission mode is per conversation. The user picks it in the composer,
and the three values are `read-only`, `workspace-write`, and
`danger-full-access`.

| Mode | What a command may do |
|---|---|
| `read-only` | May not change anything. |
| `workspace-write` | May change the workspace and nothing else. The default. |
| `danger-full-access` | Runs with no containment and no inspection. |

The mode is recorded in the typed session log as a `sandbox/mode` event, so it
survives a restart and cannot be changed out of band. The effective mode is
the per-call grant when there is one, and otherwise the strictest of the
conversation's last recorded mode, the mode the conversation's agent preset
names, and the deployment's default. The strictest mode wins, so a
conversation cannot widen the boundary the deployment set, and a preset may
only narrow it.

Two deployment settings own the default. `deepspace_sandbox_policy` is the
containment policy for the shell tools. `deepspace_permission_mode` is the
deployment-wide default that the policy falls back to. Both default to
`workspace-write`.

### The approval mode

`deepspace_approval_mode` decides whether a destructive call asks the user
first. It is `never` by default and `ask` when the deployment wants a human
decision.

The tools that need an approval when the mode is `ask` are `write`, `edit`,
`delete`, `sandbox_execute`, `fs_write`, `fs_edit`, `fs_delete`,
`fs_str_replace`, `bash`, and `pwsh`.

When a call needs an approval, the turn pauses and the product records a
pending approval that names the tool, the arguments, and the risk level. The
user answers at
`POST /api/v1/deepspace/chats/{conversation_id}/approvals/{approval_id}` with
a decision, and the turn resumes. An approval that was already answered
returns `APPROVAL_ALREADY_RESOLVED`, and an unknown one returns
`APPROVAL_NOT_FOUND`.

An extension can also name tools that always ask, whatever the deployment's
approval mode is. The extension's list is what applies, so a deployment that
set `deepspace_approval_mode` to `never` does not silence it.

### The network switch

`deepspace_sandbox_network` decides whether a command inside the cage keeps
network access. It is on by default, because a build or a package install
needs it.

| Backend | Network on | Network off |
|---|---|---|
| `bwrap` | The network namespace is shared. | The network namespace is unshared, so there is no route out. |
| `sandbox-exec` | The profile admits network calls. | The profile denies network calls. |
| `docker` | The container keeps its network. | The container runs with `--network none`. |
| `landlock` | The network is not restricted. | The network is not restricted. |
| `windows` | The network is not restricted. | The network is not restricted. |

The switch does nothing on the backends that do not control the network. A
deployment that must keep commands off the network has to use `bwrap`,
`sandbox-exec`, or `docker`.

### The read-only inspection

Before a command runs, the product reads its leading word. Under
`read-only`, a command whose first word plainly changes state is refused with
the reason and the program's name.

The programs that are always treated as mutating are `apt`, `apt-get`,
`chgrp`, `chmod`, `chown`, `cp`, `curl`, `dd`, `docker`, `git`, `init`,
`install`, `ln`, `mkdir`, `mkfs`, `mv`, `npm`, `pip`, `pip3`, `powershell`,
`pwsh`, `rm`, `rmdir`, `rsync`, `sed`, `shred`, `shutdown`, `sudo`, `tee`,
`touch`, `truncate`, `wget`, `winget`, and `yum`.

Four programs are only mutating when they carry a write flag. `sed` is
mutating with `-i` or `--in-place`. `find` is mutating with ` -delete` or
` -exec`. `xargs` is mutating when the command it runs is `rm` or `mv`. `awk`
is mutating with `>>` or `system(`.

The inspection reads only the first word. A command that reaches the same
effect by another route passes the inspection, which is why the cage, not the
inspection, is the boundary.

### Deadlines

Every tool call runs under a deadline. `deepspace_tool_timeout_seconds` is
the default and is 120 seconds. A tool that declares its own deadline runs
under that one instead. A call that overruns fails with the stable code
`TOOL_TIMEOUT`.

The shell tools have their own budget. `deepspace_shell_timeout_seconds` is
120 seconds, and the model may set the `timeout_seconds` argument of `bash`
and `pwsh` to any value up to 900 seconds. A command that overruns is
killed with its process group and the result carries `status: timeout`.

The output of a command is kept whole. A result larger than
`deepspace_spill_threshold_chars` is spilled to durable storage, and the
model reads it back with `spill_read`.

### The routes that report the cage

`GET /api/v1/deepspace/sandbox/backend` reports the cage the shell tools will
use on this host. It returns the configured backend, the selected backend,
whether that backend can cage a command, the network setting, the platform,
the enforcement level, the error code a command would fail with, and the
runners the platform offers.

`GET /api/v1/deepspace/sandbox/runtime` reports the runtime controls as a
whole: the sandbox report, the approval mode, the permission mode, whether
the shell is enabled, the concurrency budget, the spill threshold, and the
switches for programmatic calling, plan mode, the ACP bridge, model
selection, agent teams, and the remote runtime.

`GET` and `PUT /api/v1/deepspace/chats/{conversation_id}/sandbox-mode` read
and record the conversation's mode. The read returns the effective mode, the
recorded mode, the deployment default, and the list of available modes. A
mode the product does not accept is refused with the code
`SANDBOX_MODE_REJECTED`.

## What to watch for

- The Linux cage is proved on the host that runs it. The proof lives in
  `backend/tests/unit/test_deepspace_linux_cage_proof.py` and checks that a
  write inside the workspace lands on the host, a write outside never reaches
  it, a read-only workspace refuses a write, the user's home is not visible,
  and the command has no route out when the network is off.
- The same proof runs on any platform:
  `backend/tests/unit/test_deepspace_cage_proof_cross_platform.py` asks the
  product for the cage the host offers and runs the same checks against it.
  On macOS or on Windows, running that file is what produces the proof for
  those platforms; on a host with no cage it skips and says which host it
  was.
- The macOS and Windows cages have not been run against a real macOS host or
  a real Windows host in this repository, so treat the seatbelt profile and
  the restricted-token runner as unproven on those platforms until that file
  has run there.
- The Landlock runner confines file effects and nothing else. A deployment
  that uses it does not keep commands off the network.
- The Windows runner confines writes and deletes and not reads. A command can
  still read what the server user can read.
- The `bwrap` and `sandbox-exec` probes run once and their answers are
  cached, so a host that gains a backend later is picked up the next time the
  process restarts.
- The read-only inspection is a list of program names. It refuses the common
  cases and passes the rest, so `read-only` is not a substitute for a cage.
- `danger-full-access` removes both the inspection and the cage. It is a
  deliberate choice, and it applies to the shell tools and to extension code
  alike.
- A broken cage and a refused command are told apart by the result field
  `failure_kind`. `denied` means the cage did its job. `runner_failure` means
  the cage itself is broken and needs an operator.
- A command that runs without a cage is reported with `sandboxed: false`, so
  the result says plainly that no boundary was applied.
- The separate `sandbox_execute` tool is not this cage. It calls a
  separately deployed execution service over HTTP. The development defaults
  enable the local executor at `http://127.0.0.1:3020`; a production
  deployment must replace the endpoint and token, and the tool is refused when
  either value is unavailable.
