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

- A shell command runs with the local backend process's operating-system
  rights, so a command that escapes the workspace reaches everything that
  user account can reach.
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

Read-only and Workspace shell sessions always fail closed when no runner can
enforce their filesystem boundary; they return `SANDBOX_UNAVAILABLE` and list
the available runners. The legacy `deepspace_sandbox_required` setting is
reported for compatibility, but cannot disable this protection.

### The permission mode

The permission mode is per conversation. The user picks it in the composer,
and the three values are `read-only`, `workspace-write`, and
`danger-full-access`.

| Mode | What a command may do |
|---|---|
| `read-only` | Read tools remain available. Write tools are refused; inspection commands, including safe Git and PowerShell reads, run only inside a filesystem and network boundary. |
| `workspace-write` | Local file and shell changes stay inside the selected workspace. Paths and mutations outside it require Full access. The default. |
| `danger-full-access` | Local commands and file tools can reach paths the backend process's OS user can access. Destructive commands, non-inspection shell commands, and risky remote or desktop actions require direct human approval. |

The user's selection is recorded in the typed session log as a
`sandbox/mode` event. DeepSpace resolves an effective mode for each turn: the
strictest of that selection, the agent preset's restriction, and the
deployment's maximum. The deployment default starts new conversations; its
maximum caps every conversation. A preset or deployment cap can therefore
make the effective mode stricter than the selected value. The mode selector
reports both values and explains the cap. Changing the mode, preset, or
workspace closes persistent local terminal sessions before the next mode
takes effect.

`deepspace_permission_mode` is the deployment default. `deepspace_sandbox_policy`
is the deployment maximum. Server deployments default both to
`workspace-write`; the standalone desktop allows Full access as its maximum
while starting in Workspace mode.

Full access is still limited to the operating-system account running the local
backend; it does not become root or administrator. `fs_glob` and `fs_grep`
default to the selected workspace even in Full access. For user-file discovery
outside that workspace, the model should use the current user's home, `/mnt`,
or `/media`, starting with the location named by the user. It should use `/`
only when the user explicitly requests an entire-system search. Host searches
walk directories incrementally, skip protected virtual/system trees such as
`/proc`, `/sys`, `/dev`, `/run`, `/boot`, and `/root`, and continue after
unreadable directories; the result reports skipped paths. A host search must
pass an explicit absolute directory, and its result includes `search_root`. The
Codex CLI provider's temporary inference directory is not the conversation
workspace and does not determine which AverQel tools the turn may use. The
runtime status route reports whether browser automation and computer use have
their required settings and local dependencies.

### The approval mode

`deepspace_approval_mode` controls the deployment's ordinary write approval
policy. The default is `never`, so routine file edits and ordinary shell
commands do not pause. The `ask` setting remains an explicit deployment-wide
override for the write tools listed below. Risk-based confirmations are
separate from that setting and apply in Full access too.

The safety classifier asks before broad or recursive deletes, destructive Git
operations, changes to operating-system or protected user-configuration paths,
system service/package changes, and browser actions whose target or supplied
text clearly indicates a high-impact action. Page JavaScript execution and
mutating SSH actions against a separate remote host also require confirmation.
Write-capable Full access extension/plugin handlers retain their confirmation
gate. Ordinary single-file workspace deletes, routine host file edits in Full
access, common development commands, and ordinary browser interactions do not
ask solely because they modify state. Deletes outside the selected workspace,
`.git` operations, and recursive deletes still ask.

Computer-use clicks are coordinate-only, so the backend cannot reliably infer
the label or effect of the button at those coordinates. Ordinary clicks and
typing in Full access therefore do not trigger a blanket confirmation. The
classifier can recognize risky text and system-level key combinations, but it
cannot guarantee that every visually represented website or desktop action is
identified. Full access still runs as the backend process's OS user; it does
not elevate privileges.

Disk-format/wipe commands, attempts to remove core OS roots, and privilege
escalation commands such as `sudo` remain blocked even if requested. Shutdown,
reboot, and service state changes require confirmation. This prevents the
shell tool from turning Full access into administrator access or an unbounded
disk-wipe path.

The tools covered by `ask` include `write`, `edit`, `delete`,
`sandbox_execute`, `fs_write`, `fs_edit`, `fs_delete`, `fs_str_replace`,
`bash`, and `pwsh`. Mandatory safety approvals apply even when the setting is
`never`.

When a call needs an approval, the turn pauses and the product records a
pending approval that names the tool, the arguments, and the risk level. The
approval card also describes the affected path or command when it can be
identified. A recursive delete calls out that it removes the target and its
contents; review that warning and the exact target before approving. The
user answers at
`POST /api/v1/deepspace/chats/{conversation_id}/approvals/{approval_id}` with
a decision, and the turn resumes. An approval that was already answered
returns `APPROVAL_ALREADY_RESOLVED`, and an unknown one returns
`APPROVAL_NOT_FOUND`.

An extension can also name tools that always ask, whatever the deployment's
approval mode is. The extension's list is what applies, so a deployment that
set `deepspace_approval_mode` to `never` does not silence it.

### The network switch

`deepspace_sandbox_network` controls network access for Workspace commands.
Read-only shell and terminal sessions always run with network access disabled.
Full access uses the host network. Read-only and Workspace shell sessions fail
closed unless the selected runner can enforce the needed filesystem boundary;
network isolation is enforced by bubblewrap or macOS Seatbelt.

| Backend | Network on | Network off |
|---|---|---|
| `bwrap` | The network namespace is shared. | The network namespace is unshared, so there is no route out. |
| `sandbox-exec` | The profile admits network calls. | The profile denies network calls. |
| `landlock` | The network is not restricted. | The network is not restricted. |
| `windows` | The network is not restricted. | The network is not restricted. |

The switch does nothing on the backends that do not control the network. A
user who must keep commands off the network has to use `bwrap` or
`sandbox-exec` on a platform that supports it.

### The read-only inspection

Before a shell command runs, the product checks for known destructive or
privilege-elevation patterns. Read-only also rejects commands that match the
high-impact action classifier without presenting an approval card for an
action it cannot run. The operating-system sandbox is the enforcement
boundary: Read-only mounts the workspace read-only and disables networking;
Workspace hides other host paths and permits writes only in the selected
workspace. A command that cannot get the required boundary is refused with
`SANDBOX_UNAVAILABLE` rather than run uncontained.

The programs that are always treated as mutating are `apt`, `apt-get`,
`chgrp`, `chmod`, `chown`, `cp`, `curl`, `dd`, `docker`, `git`, `init`,
`install`, `ln`, `mkdir`, `mkfs`, `mv`, `npm`, `pip`, `pip3`, `powershell`,
`pwsh`, `rm`, `rmdir`, `rsync`, `sed`, `shred`, `shutdown`, `sudo`, `tee`,
`touch`, `truncate`, `wget`, `winget`, and `yum`.

Some commands are safe only with inspection-only arguments. `find` accepts a
small set of read predicates such as `-type` and `-name`; an exact trailing
`2>/dev/null` is also safe on an allowlisted read command. `find -delete`,
`-exec`, `-execdir`, `-ok`, and file-output actions require approval. Those
actions against OS roots or mounted-storage roots such as `/`, `/etc`, `/usr`,
`/mnt`, and `/media` are blocked.
Relative destructive `find` commands sent to a persistent terminal are blocked
when its current directory cannot be verified; use Bash with an explicit path
for a reviewed workspace cleanup.
Raw-device wipe and partition-format commands (including `dd`, `shred`,
`blkdiscard`, filesystem/partition formatters, and disk erase commands) are
blocked. Known commands that disable host security controls, such as turning
off the firewall, endpoint protection, mandatory access control, or execution
policy, are also blocked. Routine reads and ordinary file writes remain
available; file deletions and moves require review even when they target one
file inside the selected workspace.
`sed` is mutating with `-i` or `--in-place`. `xargs` is mutating when the
command it runs is `rm` or `mv`. `awk` is mutating with `>>` or `system(`.

Command inspection is only an extra guard. It cannot prove that arbitrary
shell syntax or a program is harmless, so the OS boundary and explicit
approvals enforce the modes.

An inspection command that is known to be read-only does not need an approval
card solely because it uses Bash or PowerShell. If the shell reports that a
requested executable is not installed or absent from `PATH`, DeepSpace reports
that fact and ends the attempt instead of sending the same failed command back
to the model for another retry.

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
the shell and persistent terminal sessions are enabled, the concurrency
budget, the spill threshold, and the switches for programmatic calling, plan
mode, the ACP bridge, model selection, agent teams, and the remote runtime.

The runtime response includes `contract_version` and `observed_at`. The
settings page refreshes it when opened, every 10 seconds while the tab is
visible, when the window regains focus, and when the operator selects Refresh.
Contract version 4 also reports read-only live activity for the current user:
worker reachability, whether the shared tool gate can reach Redis,
queued/running/failed agent counts, active agent labels, and active tool
names. Tool arguments, prompts, results, and other users' activity are not
returned. This is polling, not a push stream, so a change may
take up to one refresh interval to appear. The timestamp is when the backend
produced that response. A runner probe may be cached by the backend until it
restarts, and environment-backed deployment settings do not change inside a
running process; restart the local backend after changing its startup
configuration.

The status page distinguishes a full boundary (`bwrap` or `sandbox-exec`), a
partial boundary (`Landlock` or Windows restricted token), no usable runner,
and fields an older backend did not report. Missing fields are `Not reported`,
not a negative capability result; when a legacy response supplies its
`enforcement` level, the page derives filesystem and network-isolation support
from that level. Network-isolation support is separate from the active network
policy: a runner can support isolation while deployment policy allows network
traffic. A legacy response shows its single
`permission_mode` once instead of presenting the same value as both the
deployment default and maximum. Network policy is reported for Workspace
mode; Read-only disables network access, and Full access intentionally runs
outside the shell cage.

`GET` and `PUT /api/v1/deepspace/chats/{conversation_id}/sandbox-mode` read
and record the user's selection. The response includes `requested_mode`,
`effective_mode`, the deployment default and maximum, the active preset and
its mode, and whether a cap narrowed the selection. A mode the product does
not accept is refused with the code `SANDBOX_MODE_REJECTED`.

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
- Landlock and the Windows runner do not provide the full filesystem and
  network isolation required by Read-only and Workspace shell sessions. On
  those hosts, those shell operations fail closed; read and file tools still
  follow their own policies.
- Full access runs with the local OS account's rights; it does not elevate to
  administrator or root. It still has meaningful risk. Approvals and blocked
  commands reduce that risk but cannot guarantee that every possible program
  is harmless.
- In Full access, direct host paths are available to file tools. Writes
  outside the selected workspace and deletes always require approval. Common
  disk-wipe, privilege-elevation, shutdown, and operating-system removal
  commands are blocked.
- Remote MCP writes are blocked in Read-only and Workspace modes and require
  their connection policy's approval in Full access. Browser and computer
  actions that can change remote or desktop state require Full access and
  human approval.
- A broken cage and a refused command are told apart by the result field
  `failure_kind`. `denied` means the cage did its job. `runner_failure` means
  the cage itself is broken and needs an operator.
- A command that runs without a cage is reported with `sandboxed: false`, so
  the result says plainly that no boundary was applied.
- The optional `sandbox_execute` tool is not this cage. It calls a separately
  deployed execution service over HTTP and is disabled by default. The local
  desktop app does not bundle or start that service; its shell commands use
  the operating-system sandbox described above.
