# 11. DeepSpace native tool surface

DeepSpace gives the model one starting interface and loads capability schemas
only when a task needs them. This page describes the tool surface the model
can reach, the workspace the file and shell tools work in, the delegation
tools, and the durable records a turn leaves behind.

## Tool catalogue

The native registry currently holds 133 tools across the productivity,
workspace, web, shell, terminal, delegation, background, workflow, planning,
spill, extension, diff, session-query, scheduling, inspection, dependency, and
programmatic-calling surfaces. A plugin or connected MCP server may add more
names (see "The plugin system" and "MCP and connectors"). The registry is the
authoritative inventory; this page explains the major capability groups.

| Tool | Mode | What it does |
|---|---|---|
| `discover_tools` | read | Returns the request-authorized catalog, then loads the exact schemas the model selects. |
| `get_tool_result` | read | Retrieves a full result that a pruned tool result points at. |
| `read` | read | Reads the note, Library, memory, chat, project view, or a project workspace file. |
| `find` | read | Searches Library files, memory, or chat history. |
| `analyze` | read | Compares Library documents. |
| `web_search` | read | Searches the configured web search provider. |
| `url_read` | read | Reads a public URL as text. |
| `image_read` | read | Reads a public image URL. |
| `web_fetch` | read | Reads one public URL as text or as an image, whichever it is. |
| `fs_read` | read | Reads a workspace text file with numbered lines. |
| `fs_grep` | read | Searches workspace file contents with a regular expression. |
| `fs_glob` | read | Lists workspace paths by name pattern. |
| `list_agents` | read | Lists the compact child-agent roster; use `get_agent` for detailed results. |
| `get_agent` | read | Reads one child agent's detailed status and result. |
| `job_list` | read | Lists the background work of the conversation. |
| `job_status` | read | Reads one background job. |
| `goal_read` | read | Reads the goals of the conversation. |
| `skill_list` | read | Lists the skills installed in the workspace. |
| `skill_load` | read | Loads one skill's instructions. |
| `plan_read` | read | Reads the conversation's current plan and its review state. |
| `spill_read` | read | Reads a spilled tool result back from durable storage. |
| `extension_list` | read | Lists the extensions the conversation runs with. |
| `write` | write | Writes the note, a Library file, or a memory. |
| `edit` | write | Replaces, appends, renames, or moves a note, Library file, or project workspace file. |
| `delete` | write | Deletes a Library file, a memory, or a project workspace file. |
| `todo_write` | write | Owns the persisted task plan. |
| `plan_submit` | write | Presents a finished plan for the user to review. |
| `fs_write` | write | Creates or updates a workspace text file. |
| `fs_edit` | write | Replaces, appends, renames, or moves a workspace file. |
| `fs_str_replace` | write | Replaces one exact string inside a workspace file. |
| `fs_delete` | write | Deletes a workspace file or directory. |
| `bash` | write | Runs a shell command in the workspace. |
| `pwsh` | write | Runs a PowerShell command in the workspace. |
| `terminal_open` | write | Opens a persistent terminal session in the workspace. |
| `terminal_read` | read | Reads the buffered output of a terminal session. |
| `terminal_write` | write | Sends input to a terminal session. |
| `terminal_close` | write | Closes a terminal session. |
| `terminal_list` | read | Lists the conversation's live terminal sessions. |
| `workspace_diff` | read | Shows what the workspace's files look like now, against the baseline the turn started from. |
| `ssh_run` | write | Runs a command on a reviewed remote host over SSH. |
| `ssh_host_list` | read | Lists the reviewed remote hosts the conversation may reach. |
| `type_graph` | read | Reads the workspace's type graph: the classes, functions, methods, interfaces, and types it declares, and the references between them. |
| `schedule_create` | write | Schedules a recurring follow-up for the conversation. |
| `schedule_list` | read | Lists the conversation's schedules. |
| `schedule_update` | write | Changes one schedule. |
| `schedule_delete` | write | Removes one schedule. |
| `job_output` | read | Reads a background job's output, optionally waiting for it. |
| `read_image` | read | Reads a local image file and returns it to a vision-capable model. |
| `present` | write | Declares files that already exist as the turn's deliverables. |
| `plugin_manager` | write | Lists the deployment's plugins and enables, disables or removes one. |
| `averqel_inspect_list` | read | Lists the runtime's inspectable capabilities. |
| `averqel_inspect_query` | read | Reads one capability's tools, settings and state. |
| `list_subagent_models` | read | Lists the model routes a delegated child may use. |
| `load_workspace_dependencies` | read | Reports the absolute paths of the libraries and binaries the deployment ships. |
| `session_search` | read | Searches the caller's earlier conversations for work they did. |
| `session_event_search` | read | Searches one conversation's events. |
| `session_event_read` | read | Reads one event with its neighbours. |
| `session_trace` | read | Reads one conversation's timeline. |
| `session_event_trace` | read | Traces one event's lineage. |
| `terminal_signal` | write | Sends a signal to a terminal's foreground process group. |
| `terminal_job` | read | Reads a background terminal write. |
| `list_mcp_resources` | read | Lists the resources the connected MCP servers expose. |
| `list_mcp_resource_templates` | read | Lists the parameterised MCP resource templates. |
| `read_mcp_resource` | read | Reads one MCP resource by URI. |
| `ssh_fs_read` | read | Reads a file on a reviewed remote host. |
| `ssh_fs_write` | write | Writes a file on a reviewed remote host, atomically. |
| `ssh_fs_list` | read | Lists a directory on a reviewed remote host. |
| `ssh_fs_delete` | write | Removes a file on a reviewed remote host. |
| `ssh_terminal_open` | write | Opens a persistent terminal on a reviewed remote host. |
| `ssh_terminal_read` | read | Reads the output of a remote terminal. |
| `ssh_terminal_write` | write | Sends input to a remote terminal. |
| `ssh_terminal_signal` | write | Signals a remote terminal's foreground process group. |
| `ssh_terminal_close` | write | Closes a remote terminal. |
| `ssh_stream_read` | read | Reads the output of a streaming remote command. |
| `ssh_stream_close` | write | Closes a streaming remote command. |
| `spawn_teammate` | write | Creates a named teammate of the conversation. |
| `team_send_message` | write | Sends a message to a named teammate. |
| `list_teammates` | read | Compatibility detail view of the conversation's named teammates. |
| `team_list_members` | read | Lists the canonical compact named-teammate roster. |
| `team_get_member` | read | Reads one named teammate's detailed result. |
| `wait_agent` | read | Waits for a teammate, a message, or a task to change. |
| `team_task_create` | write | Adds a task to the shared team board. |
| `team_task_list` | read | Reads the shared team board. |
| `team_task_update` | write | Changes a task on the shared team board. |
| `runtime_discover` | read | Lists what the runtime can do: its tools, plugins, extensions and routes. |
| `hook_run` | read | Runs one of the deployment's hooks and reports its decision. |
| `run_code` | write | Runs one program that calls DeepSpace tools as ordinary function calls. |
| `sandbox_execute` | write | Runs bounded Python, SQL, Bash, or PowerShell in the isolated sandbox. |
| `artifact_create` | write | Saves a structured artifact in the Library. |
| `extension_write` | write | Installs or changes one extension for the conversation. |
| `extension_remove` | write | Removes one extension from the conversation. |
| `subagent` | write | Delegates a self-contained task to a child agent. |
| `subagent_fork` | write | Delegates a task to a child that inherits the conversation history. |
| `send_message` | write | Queues a message for a continuable child. |
| `interrupt_agent` | write | Requests cancellation of a child's current turn. |
| `job_kill` | write | Cancels one background job. |
| `goal_set` | write | Creates, updates, or finishes a goal. |
| `workflow_run` | write | Runs a workflow's steps as child agents. |
| `ralph_run` | write | Runs a script over repeated fresh agents. |
| `ask_user` | interactive | Requests required input from the user. |

Every tool is declared once, in the module that owns its behaviour, under
`backend/app/deepspace/services/tools/`. The declaration carries the
provider-facing schema, the typed argument model, the execution mode, and the
handler. The registry, the policy allowlist, and the dispatcher all read that
one declaration, so a tool cannot be offered to the model without a policy
mode and cannot have a policy mode without a handler.

## The project workspace

The Library is conversation-scoped: every file is a database row and every read
is authorized against one conversation. The file and shell tools use a
different storage, because a coding agent works on a directory tree that
outlives one conversation.

The workspace root is `<deepspace_workspace_root>/<tenant id>/<user id>`. An
empty `deepspace_workspace_root` uses `<local_storage_path>/workspaces`. Every
path a tool resolves is normalized, joined to the root, and checked again
after symlink resolution, so neither a `..` segment nor a symlink inside the
workspace reaches a file the tenant does not own. A path that leaves the root
is a refusal the model can act on, not a crash.

The `fs_` tools and the `read`, `write`, `edit`, and `delete` tools address
different things. The `fs_` tools address the workspace directory tree. The
others address the conversation's Library, memory, and note.

`read`, `edit`, and `delete` also accept `target="workspace"` with a `path`,
so the unified tools reach the project workspace as well as the Library. The
`read` tool names the workspace line budget `line_limit`, because its
`limit` already means something else for a Library query.

## Content-addressed attachments

A local upload is stored by its SHA-256 fingerprint. The blob lives at
`<bucket>/<tenant id>/attachments/<sha256>` and the document key is a hard
link to it, so two uploads of identical bytes share one inode and one copy on
disk. The document row keeps its own key, so existing reads, versions, and
deletes work unchanged. Deleting one document drops that link; the blob
remains while another link points at it. The single-file delete endpoint
prunes the one blob it may have orphaned; deleting a folder or a conversation
runs the full tenant sweep. `prune_unreferenced_blobs` removes the blobs whose
last link is gone.

`ContentAddressedAttachmentStore` returns an `AttachmentRef` with the
fingerprint, size, media type, bucket, and key. The store is idempotent:
repeating a put returns the same reference and writes no second object. A
local upload records that reference in `metadata_json.content_ref`, so a
reader can address the content without knowing the document key.

Hard links are a local-filesystem feature. On MinIO or S3 the explicit
attachment store still deduplicates by key; per-document uploads keep one
object per document because object storage has no link operation.

## Shell execution and the permission mode

Two tiers run commands.

`sandbox_execute` sends the code to the configured sandbox executor. It
accepts `python`, `sql`, `bash`, and `pwsh`, and it is never run in the API or
worker container. This checkout enables the separate sandbox by default and
points local development at `http://127.0.0.1:3020`; a deployment may disable
it or replace that endpoint and token. The executor's actual network and
filesystem isolation are deployment responsibilities, so verify its status
before treating the tool as available.

`bash` and `pwsh` run the command in the worker process. They are on by
default and the deployment's `deepspace_sandbox_policy` decides how much of
the host a command may reach. When that setting is empty, the broader
`deepspace_permission_mode` supplies the mode:

| Mode | Behavior |
|---|---|
| `read-only` | A command whose leading program plainly mutates state is refused, and the cage mounts the workspace read-only. |
| `workspace-write` | The command starts inside the tenant workspace, which the cage makes the only writable path. This is the default. |
| `danger-full-access` | The command runs with no inspection and no cage. |

The permission mode inspects the command line. A second layer runs the
command inside an operating-system cage whenever the host offers one:

| Backend | Platform | What it contains |
|---|---|---|
| `bwrap` | Linux | A private mount, PID, IPC, UTS, and cgroup namespace. Only system directories are readable; the workspace is the only writable path; the user's home directory is hidden. |
| `landlock` | Linux | A kernel-LSM ruleset applied by path, with no namespace. It is the runner for a host where unprivileged user namespaces are unavailable and bubblewrap therefore cannot work. It confines file effects only, so it does not restrict the network. |
| `sandbox-exec` | macOS | A seatbelt profile that denies everything except the system directories, the workspace, and temporary files. |
| `windows-acl` | Windows | A restricted token whose restricting SIDs carry a capability SID that only the workspace grants, at Low integrity, inside a job object that dies with the runner. A write has to satisfy the user's own rights *and* the capability grant, so a write elsewhere fails. The boundary is partial: it confines writes and deletes, not reads and not the network. |
| `docker` | Any platform with Docker | A container with the workspace mounted and no network unless `deepspace_sandbox_network` is on. Requires `deepspace_sandbox_image`. |
| `none` | Any platform | No cage. The inspection layer still runs. |

Linux needs the `bubblewrap` package; macOS ships `sandbox-exec`; Windows
ships PowerShell; Docker needs an engine and an image with the shell.
`deepspace_sandbox_backend=auto` picks the best available backend for the
host: bubblewrap and then Landlock on Linux, the seatbelt sandbox on macOS,
the restricted-token runner on Windows, and Docker when an image is
configured. For `read-only`, the workspace bind is read-only (bwrap
`--ro-bind`, Docker `readonly`, or a seatbelt profile without a write
rule), so a shell redirection the inspection misses still cannot write.
Network access inside the cage is controlled by
`deepspace_sandbox_network`; it stays on by default so local builds and
package installs keep working.
`deepspace_sandbox_required=true` refuses a command when no backend is
available instead of silently running it uncontained. Because the user's
home directory is hidden, a command that needs `~/.ssh`, `~/.gitconfig`, or
a global package cache must use a workspace-local copy or a credential
placed inside the workspace.

The Linux cage is **proved on the host that runs it**, not only inspected:
a command inside the cage writes in its workspace and the file lands on the
host; a write outside the workspace never reaches the host; a read-only
workspace refuses a write; the user's home directory is not the command's to
read; and the command has no route out when the deployment forbids the
network. The proof lives in
`backend/tests/unit/test_deepspace_linux_cage_proof.py`, and it skips with
its reason on a host that cannot create the namespaces.

Every runner is **probed** before it is used, and a runner that cannot
confine anything is reported as unavailable rather than trusted. A command
that cannot be confined is refused with the typed code
`SANDBOX_UNAVAILABLE`, and the refusal names the runners the platform
offers, so the operator knows what to install.

A command's failure is also **classified**: a policy denial
(`failure_kind` of `denied`) is the sandbox doing its job, while a broken
sandbox (`failure_kind` of `runner_failure`) is reported separately, so the
two are never confused. `GET /api/v1/deepspace/sandbox/backend` reports
the configured and selected backend, the platform, the enforcement level,
and the typed error a command would fail with.
`GET /api/v1/deepspace/sandbox/runtime` reports the whole runtime control
set.

## The sandbox mode of a conversation

The sandbox mode says how much of the host a shell command may reach. It
is a **per-conversation** state, not a deployment-wide one, so two
conversations of the same user never share a boundary, and a user can
tighten one conversation without changing the deployment.

The state is recorded in the typed session log as a `sandbox/mode` event,
so it survives a restart by replay and cannot be changed out of band. The
effective mode for a call is the per-call grant when there is one, then
the conversation's last recorded mode, then the deployment's configured
default. The mode governs both the shell tools and the native write
tools, so a `read-only` conversation refuses `write`, `edit`, and `delete`
as well as the shell commands that would change state.

`GET /api/v1/deepspace/chats/{id}/sandbox-mode` reads the conversation's
mode and `PUT /api/v1/deepspace/chats/{id}/sandbox-mode` records a new
one. The composer offers the choice directly.

The effective mode is also **model-visible**. Each turn carries a
`Sandbox mode: <mode>` section in its system prompt that states what the
conversation may change and where the workspace is, so the agent plans
inside the boundary it has instead of discovering it by being refused.

## Delegation

A root agent can start a child. The child's own tools are the parent's tools,
so a child's work behaves exactly like the parent's work.

| | `subagent` | `subagent_fork` |
|---|---|---|
| Context | Fresh | Inherits the parent's recent history |
| Mode | `continuable` by default; `one-shot` on request | `one-shot` |
| Continuation | `send_message` queues a further turn | Not available |
| Model | May name its own provider and model | Inherits the parent's route |
| Tools | May be filtered by name | Inherits the parent's tools |
| Depth | Counted from the root agent | Counted from the root agent |

A child is a durable record in `deepspace_subagent_runs`, not a transient
object. The record stores the child's provider, model, reasoning effort,
persona, tool restriction, and a versioned `subagent/descriptor` payload, so a
continuable child can be continued later without the parent repeating every
parameter. A child below the deployment's depth limit receives the delegation
tools too, so a root can delegate to a child that delegates once more; the
same depth check stops the chain at the configured limit.

`send_message` queues the message and dispatches the child's turn to the
worker, so the turn runs outside the parent's tool call and survives it. The
child keeps its own exchanges in `turn_history`, so a follow-up turn answers
in the context the child already built. Because the child record is durable,
a child continued from another process resumes with the composition its
descriptor recorded.

`deepspace_subagent_max_depth` bounds the nesting, and
`deepspace_subagent_max_children` bounds how many children one conversation
may hold at once.

`workflow_run` runs the steps of a workflow document as child agents, in
dependency order. The document lives at
`<workspace>/.averqel/workflows/<name>.json` and names the steps and the
dependencies between them.

`ralph_run` runs a script over repeated fresh agents until one reports
`RALPH_DONE` or the round budget is spent. The script lives in the workspace
and the runner refuses to start without one.

## Plan mode

Plan mode asks the agent to design before it acts. While a conversation is
in plan mode the runtime offers only the read-only tools plus `plan_submit`,
so the agent can research and inspect but cannot change anything, and it
receives the plan-mode guidance in its system prompt.

The mode is a per-conversation state, not a per-turn one. It is recorded in
the typed session log as a `plan/mode` event, and the last recorded mode
change decides it, so the mode survives a new turn, a compaction, and a
restart. A conversation that has a plan under review but never recorded a
mode change is in plan mode too, because the agent is waiting for the
review.

The plan itself is a durable row in `deepspace_plans` with a title, a
Markdown body, a review status, and the user's feedback. `plan_submit`
records the proposal, ends the turn with a `plan_submitted` frame, and
pauses the run at `awaiting_plan_review` so the work does not start before
the review. The user then approves the plan, asks for changes with
feedback, or rejects it; the decision is recorded as a `plan/reviewed`
event, and a change request keeps the conversation in plan mode while the
agent revises.

The routes are `GET /api/v1/deepspace/chats/{id}/plan`,
`PUT /api/v1/deepspace/chats/{id}/plan/mode`, and
`POST /api/v1/deepspace/chats/{id}/plan/review`.

## Programmatic tool calling

`run_code` lets the model write one program that calls host tools as
ordinary function calls. The program receives a `tool` function, so
`tool('web_search', {'query': '...'})` returns that tool's result, and the
program returns only what it printed and what it returned. One program can
replace a long chain of provider round trips.

The tool is offered only in the `ptc` mode, because programmatic calling is
off in every other mode.

The program runs in a child process, never in the API or worker process, so
a failing program cannot take the turn down. The child reaches the host
over a request/response channel on its standard streams: it writes a
request frame, the host runs the requested tool through the same policy
and tenant scope as any other call, and the host writes the result back.
The bridge forwards the program's prints as frames on the same channel, so
the output and the tool requests keep their order.

The child is confined by the operating-system cage the deployment already
uses, and it has no network. Programmatic tool calling has no cage
fallback: without one, the program would run with the worker's own
privileges, so the runtime refuses the call.

The program may only call the tools the model named, and the host enforces
that list: a call to any other tool is refused before it runs. The
deployment's own policy still applies to every call the program makes.

`deepspace_ptc_enabled` switches the capability off, and
`deepspace_ptc_timeout_seconds` and `deepspace_ptc_max_tool_calls` bound
one run.

## Spilling large results

A tool result that is far larger than the context window is offloaded to
durable, content-addressed storage instead of being carried in the model's
context. The spilled bytes live at `<bucket>/<tenant id>/spill/<sha256>`, so
two spills of the same result share one object, and the model receives a
preview plus a reference it can read back with `spill_read`.

The spill is what remains when the result is gone from the context, so it
is stored in the tenant's object storage rather than in the transient
tool-result store. `deepspace_spill_threshold_chars` is the ceiling above
which a result is spilled, and `deepspace_spill_max_chars` is the largest
result one spill accepts.

## Extensions

An extension changes how the agent works without changing the product. It
adds guidance to the system prompt, registers an alias for a tool the model
already has, or tightens the tool policy for one conversation.

Extensions are declarative. A manifest is a JSON document in the
conversation's workspace under `.deepspace/extensions/`, so the agent can
install and change one itself: that is the runtime self-modification the
surface offers. A declarative manifest is the right shape for that, because
a manifest is data the runtime applies, while an imported module would be
code running inside the API process with the worker's own privileges.

The deployment's own policy always wins. An extension may narrow what the
agent may do, and it may add guidance, but it cannot widen the
deployment's approval mode, its sandbox policy, or its tool allowlist. A
denied tool stays refused even when the deployment would allow it, and a
tool an extension marks for approval always asks, whatever the
deployment's approval mode is.

An alias publishes the target tool's parameters except the ones the alias
fixes, so the model cannot change them. The fixed arguments are applied
when the call runs, and the target's own contract still validates the
result.

`extension_list`, `extension_write`, and `extension_remove` manage the
manifests.

## The Agent Client Protocol bridge

The bridge lets a trusted program drive DeepSpace conversations over
JSON-RPC 2.0 at `POST /api/v1/deepspace/acp`. It implements the protocol's
session methods: `initialize`, `authenticate`, `session/new`,
`session/load`, `session/prompt`, `session/cancel`, `session/list`, and
`session/close`.

The bridge is a thin protocol layer. It owns the session-to-conversation
mapping, the prompt content it accepts, and the update shapes it emits; the
turn itself is the ordinary DeepSpace turn, so the same tools, policy,
durable log, and projections apply. A client that drives DeepSpace through
the bridge gets exactly the behavior the browser gets.

A prompt carries text blocks. The updates a turn produces are
`agent_message_chunk`, `tool_call`, `tool_call_update`, and `plan`. The
bridge is off unless `deepspace_acp_enabled` is set.

## Persistent terminals

A shell command runs and ends.  A terminal session keeps a shell alive, so
the agent can run an interactive program, watch a server start, or send
keystrokes into a process it started.

| Tool | Mode | What it does |
|---|---|---|
| `terminal_open` | write | Opens a session with `bash`, `pwsh`, or `cmd`, optionally in a workspace subdirectory, optionally running a command at startup. |
| `terminal_read` | read | Reads the buffered output, and says how much was dropped when the buffer overflowed. |
| `terminal_write` | write | Sends input to the session. |
| `terminal_close` | write | Closes the session and the process behind it. |
| `terminal_list` | read | Lists the conversation's live sessions. |

On a POSIX host the session gets a real pseudo-terminal, so a program that
reads from a terminal behaves the way it does on a desktop.  On Windows
there is no pseudo-terminal without a third-party package, so the session
runs over pipes and reports `pty: false`.

A session belongs to one tenant, one user, and one conversation.  Another
tenant's session id is refused exactly like a session that does not exist,
so the id never reveals that it exists.  The session's working directory is
inside the conversation's workspace, and its environment carries the paths
a command needs and none of the deployment's secrets.  A session that is
idle for longer than `deepspace_terminal_idle_timeout_seconds` is closed.

The same sessions and the same workspace are served to the chat's side
panel over `GET`/`POST` `/api/v1/deepspace/chats/{id}/terminals`,
`GET`/`POST`/`DELETE` `/api/v1/deepspace/chats/{id}/terminals/{session_id}`, and
`GET` `/api/v1/deepspace/chats/{id}/workspace/tree` and
`GET` `/api/v1/deepspace/chats/{id}/workspace/file`.  The panel is another caller
of the same service, so a session the user opens and one the agent opens
are the same kind of session, under the same tenant, user, and
conversation rules.

## The workspaces a conversation works in

An account owns a **default workspace** - the directory the file and shell
tools already used - and any number of **named workspaces** beside it.  A
conversation names the workspace it works in, so two conversations of the
same user can work in different directories at the same time.

A named workspace sits beside the default one rather than inside it, so a
workspace never appears in another workspace's file tree and a
conversation working in one cannot reach the others through the file
tools.

| Route | What it does |
|---|---|
| `GET /api/v1/deepspace/chats/workspaces` | Lists the account's workspaces: the default one and every named one. |
| `POST /api/v1/deepspace/chats/workspaces` | Creates a named workspace from `{"name": "..."}`.  A name that could name a path outside the account's root is refused, and so is a second workspace of the same name. |
| `GET /api/v1/deepspace/chats/{id}/workspace` | Reads the workspace the conversation works in. |
| `PUT /api/v1/deepspace/chats/{id}/workspace` | Records the workspace the conversation works in. |

The choice is recorded in the typed session log as a `workspace/selected`
event, so it survives a restart by replay and cannot be changed out of
band.  The workspace a call runs in is resolved once per turn and reaches
every tool that touches files: the file tools, the file search, the shell,
the terminal sessions, the diff, the skills, the workflows, the type
graph, the extensions, and the plugins' own code.  A call therefore cannot
reach outside the workspace the conversation chose.  A workspace belongs
to one tenant and one user: another account's workspace id is refused
exactly like one that does not exist.

The chat's composer offers the same choice as a **workspace** menu beside
the sandbox mode, with an **Add workspace…** entry that makes a new one.

The same managed subprocess layer backs the one-shot shell tool, so both
share one set of rules about output, timeouts, and the environment.

## The workspace panel

The chat carries a right-hand panel with three tabs.

* **Files** lists the conversation's workspace and opens a text file.
  A path that leaves the workspace is refused.
* **Terminal** shows the conversation's live terminal sessions, and lets
  the user read their output and send input.
* **Browser** opens a page inside the app, so the user can look at the
  thing they are building.  Only `http` and `https` are accepted, and a
  host outside the loopback addresses needs an explicit opt-in.  The frame
  is sandboxed without `allow-same-origin`, so a framed page cannot reach
  this app's origin.

## The code diff

`workspace_diff` shows what the workspace looks like now.  A workspace that
is a git repository is diffed with git, which gives a line-level diff.  A
workspace that is not is diffed against the baseline the turn started
from, which shows which files were added, removed, and changed.

## The type graph

A type graph is the map of what a codebase defines and what refers to
what. `type_graph` reads it from the workspace's own source, so it never
leaves the workspace.

It holds the classes, functions, methods, interfaces, types, and module
constants a workspace declares, and the references between them. Give it a
name and it answers where that name is defined and what refers to it:

```json
{"status": "ok", "name": "Widget",
 "definitions": [{"name": "Widget", "kind": "class", "path": "src/widget.ts", "line": 12}],
 "references": [{"source": "render", "path": "src/app.tsx", "line": 40}]}
```

Without a name it returns the whole graph, together with a fingerprint
that changes when the declarations change, so a caller can tell whether a
cached graph is still current.

Python is parsed with the standard library's own parser, so the
declarations and the references are exact. TypeScript and JavaScript are
read from their source text, because the product carries no parser for
those languages; the reading is deliberately shallow and never claims
more than it found.

## Tool hooks

A hook watches the agent's tool calls and may refuse one. The events are
the Claude Code and Codex names: `PreToolUse` runs before a call and may
block it, `PostToolUse` runs after a call and may add context, `Stop` runs
when a turn ends, and `SessionStart` runs when a conversation starts.

A hook is a command that answers with one JSON object on stdout:

```json
{"decision": "block", "reason": "that would delete production data"}
```

The decision is `allow`, `block`, or `ask`. The hook receives the call on
stdin as `{"event": "PreToolUse", "tool_name": "fs_write",
"arguments": {...}, "conversation_id": "..."}`. Its environment carries the
paths a command needs and none of the deployment's secrets.

A broken hook never blocks the agent: a hook that times out, crashes,
answers with invalid JSON, or exits non-zero is treated as allowing the
call.

The deployment declares its hooks in a manifest:

```json
{"hooks": [{"id": "block-prod-writes", "event": "PreToolUse",
            "command": ["python3", "guard.py"], "matcher": "fs_write"}]}
```

`deepspace_hook_manifest` points at that file or at a directory of them.
The `matcher` is the tool the hook watches, and an absent matcher watches
every call. A `PreToolUse` hook that blocks a call stops it before the
tool runs, and the hook's reason is what the model is told.

## Remote hosts over SSH

A host is a machine someone has reviewed: where it lives, who runs the
command on it, the host key the connection must present, and the private
key that identifies the client. Two rules make the remote boundary safe.

* **The host key is checked.** The connection is made against a
  `known_hosts` file built from the reviewed host's own entry, with
  `StrictHostKeyChecking` on and no interactive confirmation. A machine
  that answers for a reviewed name with a different key is refused, and
  the refusal is typed: `SSH_HOST_UNTRUSTED`.
* **The key never leaves the server.** The database holds the *name* of a
  key, never the key. The key is read from the path the deployment
  resolves, and a key file other users can read is refused.

A command runs in the host's reviewed remote directory, with a timeout and
an output cap, and the deployment's secrets never reach the remote shell.
The environment the connection carries is the paths a command needs and
nothing else.

Hosts are per tenant and per user. `GET`/`POST /api/v1/deepspace/ssh-hosts`
lists and adds them, and `PATCH`/`DELETE /api/v1/deepspace/ssh-hosts/{id}`
enable, disable, re-point, and remove one. Adding a host needs the host
key it must present, so a host without one is refused.

## The loop guard

An agent that calls the same tool with the same arguments over and over
burns tokens and never finishes.  The guard stops that.

| Limit | Setting | Default |
|---|---|---|
| Tool calls in one turn | `deepspace_loop_guard_max_tool_calls` | 64 |
| The same call, repeated | `deepspace_loop_guard_max_identical_calls` | 4 |
| The same arguments, under any tool | `deepspace_loop_guard_max_repeated_arguments` | 6 |
| The wall-clock budget of one turn | `deepspace_loop_guard_max_turn_seconds` | 900 |

A refused call carries the typed code `LOOP_GUARD_TRIPPED` and says which
limit was reached, so the agent can change what it is doing rather than
repeat the call.  The counters are per turn, so a long conversation is not
stopped by the work of an earlier turn.

## The model a conversation runs on

A conversation can name the model it runs on, so two conversations in the
same workspace can use different models. The choice is recorded in the
typed session log as a `model/selection` event, so it survives a restart
by replay. `GET`/`PUT /api/v1/deepspace/chats/{id}/model` read and record
it.

The deployment's assignment still decides the provider. The conversation
chooses the model within that provider, and a selection that names a
provider the deployment does not offer the conversation leaves the
deployment's choice standing.

## The agent presets

A preset says how an agent runs: which tools it has, whether programmatic
tool calling is on, how deep it may delegate, which sandbox mode it starts
in, and how much reasoning it spends.

| Preset | What it is for |
|---|---|
| `standard` | The default. Every tool the policy allows, subagents allowed, no programmatic calling. |
| `ptc` | The programmatic mode. The agent writes one program that calls the tools as functions. |
| `minimal` | A small, safe set for quick questions: read-only tools, no shell, no subagents, read-only sandbox. |
| `creator` | Content work: notes, Library files, memory, artifacts. No shell, no subagents. |
| `custom` | Whatever the user configures, with the same shape. |

The preset a conversation runs in is recorded in the typed session log as a
`preset/mode` event, so it survives a restart.  `GET
/api/v1/deepspace/chats/presets` publishes the catalogue, and `GET`/`PUT
/api/v1/deepspace/chats/{id}/preset` read and record the conversation's
preset.  The preset's tool set is applied by the tool policy, so a tool the
preset does not include is refused with a message that says why.  The rest of
the preset is applied by the turn as well: the delegation depth, the sandbox
mode, the reasoning effort, and the programmatic-calling flag.  The
`custom` preset carries the settings the user edited, and they are recorded
with the mode.  The chat's composer offers the same catalogue as an **agent
mode** menu beside the sandbox mode, so a user can change the mode without
knowing the API.

## Searching the session log

The typed session log is the durable record of what the agent did. Five
tools read it, so the agent can recall what it already did instead of
re-deriving it or asking the user.

| Tool | What it answers |
|---|---|
| `session_search` | Which of my earlier conversations contain this? |
| `session_event_search` | Which events of this conversation match? |
| `session_event_read` | What exactly did this event say, and what surrounded it? |
| `session_trace` | What did this conversation do, turn by turn? |
| `session_event_trace` | What are the turns, steps and events around this one? |

Every read is scoped to the tenant and the user that owns the
conversation, and a conversation that is not theirs is reported as not
found rather than as forbidden, so the tools never reveal that it exists.
The same reads are available over HTTP:
`GET /api/v1/deepspace/chats/session-query/sessions`,
`GET /api/v1/deepspace/chats/{id}/session-query/events`, and
`GET /api/v1/deepspace/chats/{id}/session-query/trace`.

## What a turn changed, and the deliverables

Every turn records the files it changed, with the number of changed lines
each one carries. The record is per turn, so the agent can report what
*this* turn did rather than what the workspace looks like now.

`present` declares files that already exist as the turn's deliverables,
so the user can open exactly what the agent produced. It refuses a path
that does not exist or that is outside the workspace, and it is
all-or-nothing: one bad path refuses the whole call.

## The shell keeps its whole output

A command that prints more than the model context holds does not lose the
rest. The output is written to durable storage, the model receives a
preview plus a reference, and it reads the rest back in pages with
`spill_read`. `deepspace_shell_capture_bytes` (8 MB by default) is the
largest output that is kept; past it, the result says how much was
dropped.

## Every tool call has a deadline

`deepspace_tool_timeout_seconds` (120 seconds by default) bounds every
native tool call. A tool that exceeds its deadline returns the pipeline's
timeout result with the typed code `TOOL_TIMEOUT`, so a hung call ends
the turn's work instead of hanging it. A tool may declare a longer or
shorter deadline of its own, and `bash` and `pwsh` keep their own
wall-clock budget.

## Edits that notice a concurrent change

`fs_edit` and `fs_str_replace` take an optional `expected_version`: the
hash of the file the agent read. When the file on disk has changed since,
the edit is refused with `FS_STALE_VERSION` and nothing is written, and a
file that is gone is refused with `FS_AMBIGUOUS_EDIT`. The guard is
opt-in, so an edit that does not name a version behaves exactly as it did
before. `fs_read` and `fs_write` report the version, so the agent can
name it on the next edit.

## MCP resources

The MCP surface reached tools only. Three tools read **resources** now:
`list_mcp_resources` and `list_mcp_resource_templates` show what the
connected servers publish, and `read_mcp_resource` reads one by URI. A
binary resource comes back base64-encoded with its media type.

The reach is the connection's own scope: the servers the conversation is
connected to, that the deployment has approved and enabled. A server that
is switched off for the conversation is unreachable, and a resource no
server publishes is refused. The three tools only appear in the catalog
when the conversation has a server connected, so they are never offered and
then refused.

The answers a real server gives are the answers the product handles. A
server that publishes tools but no resources answers the resource listings
with "method not found", which is not a failure: the listing is empty. A
server that does not publish a URI says so in several ways - the
specification's "resource not found" code, the "method not found" code, or
a plain message such as "Unknown resource: ..." - and the read is refused
for all of them. The read path is proved against real MCP servers, not only
against stand-ins, in
`backend/tests/integration/test_deepspace_mcp_resources_live.py`.

## Named teammates

A conversation can run a small team instead of one agent. Each teammate has
a name, its own provider and model, a persona, and the tools it may use.
The agent addresses it by name.

| Tool | What it does |
|---|---|
| `spawn_teammate` | Creates a named teammate and prepares its composition. |
| `team_send_message` | Sends a message to a teammate. The message is queued for the teammate and the teammate's turn is dispatched, so the teammate works in the background whether or not it was running. |
| `list_teammates` | Compatibility detail view of the conversation's teammates, their state, and what each one last reported. |
| `team_list_members` | Lists the canonical compact teammate roster and durable revision. |
| `team_get_member` | Reads one teammate's detailed status and result. |
| `wait_agent` | Waits until a teammate, a message, or a task changes. |
| `team_task_create` / `team_task_list` / `team_task_update` | The shared task board. An update names the revision it expects, so two teammates cannot silently overwrite each other. |

A teammate is a durable delegated child with a name, so it survives a
restart, and its messages wait for it while it is away. The team board and
the messages are recorded in the typed session log.

A message reaches the teammate through the child run's own turn queue, so a
teammate that is not running still answers it: the queue is the teammate's
mailbox, and the worker claims it when the teammate's turn starts. The
teammate's answer is read with `team_get_member`, or with the compatibility
`list_teammates` detail view, which carries the last result the teammate reported.

## The remote runtime

A reviewed remote host can carry the whole tool surface, not one command.
`ssh_fs_read`, `ssh_fs_write`, `ssh_fs_list` and `ssh_fs_delete` read and
write the host's files, and `ssh_terminal_open`, `ssh_terminal_read`,
`ssh_terminal_write`, `ssh_terminal_signal` and `ssh_terminal_close` hold a
terminal on the host. `ssh_run` can stream a command's output as it
arrives.

The remote path is confined twice: the server refuses a path that leaves the
workspace, and the host resolves every link it meets and refuses anything
that lands outside the reviewed directory. A write is a temporary file plus
an atomic rename, so a half-written file is never left behind. The trust
rules are the ones the command runner already uses: the reviewed host key,
the key that never leaves the server, and a reduced environment.

`deepspace_ssh_runtime_enabled` switches the whole remote surface off.

## The model a conversation runs on

A conversation either names the model it runs on, or lets DeepSpace choose.
The choice is per conversation, and the deployment decides the default
(`deepspace_model_selection_mode`).

When DeepSpace chooses, the rule is explainable: a turn with an image wants
a model that reads images, a turn that calls tools wants one that calls
tools, and a long conversation wants one whose context holds it. The same
turn always picks the same model.

A manual choice may name **any provider** the tenant may use, not only the
provider the workspace is assigned to. The composer's model menu has a
**Let DeepSpace choose** switch for it, and
`GET`/`PUT /api/v1/deepspace/chats/{id}/model` read and record the choice.

## Extensions that run code

An extension may be declarative - guidance, a tool alias, a refused tool -
or it may carry a handler. A python handler is a file in the conversation's
workspace; a command handler runs a command in the workspace with the
reduced environment and a timeout. The deployment's policy wins:
`deepspace_plugin_code_enabled` switches extension code off entirely.

Extension code runs **in its own process, inside the deployment's cage**,
never in the server process. The cage is the same one the shell tool uses:
bubblewrap, Landlock, the macOS seatbelt profile, or a container. The
conversation's own containment mode governs it, exactly as it governs the
shell commands: a read-only conversation gets a read-only workspace, and a
conversation that asked for full access runs without a cage - but still in
its own process. The handler is imported from the workspace by the system
interpreter - not the deployment's own - so it cannot read the server's
memory, its environment or the application's code, and it cannot write
outside the workspace. The handler's arguments arrive as JSON and its
result leaves as JSON, so a handler that hangs is stopped at its budget, a
handler that raises fails its call, and neither can take the server down. A
host that offers no cage runs no confined extension code: the call is
refused with a message that says so.

A handler that takes a second argument receives the workspace, the
conversation and the tenant as plain data, because it no longer shares a
process with the server. The context answers to both `context["workspace"]`
and `context.workspace`, so a handler written for the in-process contract
keeps working.

A plugin that speaks another language over JSON-RPC is confined the same
way: its command runs in the cage, in the workspace, with the workspace as
its home directory.

A plugin found under `<workspace>/.deepspace/plugins/` belongs to that
workspace, and `runtime_discover` tells the model what the runtime can do
without running anything.

## Tool hooks in the Claude Code and Codex dialect

A hook may answer in DeepSpace's own shape
(`{"decision": "allow" | "block" | "ask"}`) or in the Claude Code and Codex
shape (`{"hookSpecificOutput": {"permissionDecision": "deny"}}`), and a hook
that exits with code 2 and the reason on stderr blocks the call. The
matcher is a literal tool name, or a regular expression when the manifest
says so.

An ask is not dropped. A hook that asks about a call does not refuse it:
the call does not run, and the turn pauses with an approval request that
carries the hook's own question, so the user decides. When the user
approves, the call runs and the hooks are **not** asked about it again, so
an approval can never become a loop of approvals. Every hook invocation
writes two events to the typed session log - that it ran, and what it
decided - so a block is reconstructable from a recorded session.
`PostToolUse`, `Stop` and `SessionStart` hooks run on their own events, and
their context reaches the model.

## The plugin system

A plugin adds behaviour to the agent without changing the product.  It
declares itself in a manifest, and the product reads three sources: the
built-in plugins, the deployment's own plugin directory, and an installed
Python package that declares a `deepspace.plugins` entry point.

A manifest is data, never code, and it is validated before the product
reads a field of it.  It names the tools the plugin offers, the guidance it
adds to the system prompt, the tools it refuses, and how it runs:

* a Python handler, `module:function`, called with the same arguments a
  native tool handler receives, and
* a command that speaks JSON-RPC over stdio, which is how a plugin
  written in another language joins the product.  An AverQel plugin uses
  this shape.

Whether a plugin is on is a per-conversation state, recorded in the typed
session log as a `plugins/enabled` event, so it survives a restart by
replay.  `GET /api/v1/deepspace/chats/plugins` publishes the catalogue, and
`GET`/`PUT /api/v1/deepspace/chats/{id}/plugins` read and record the
conversation's plugins.  The deployment's policy always wins: a plugin the deployment did
not enable cannot be switched on from a conversation, and a plugin's deny
list always wins over its allow list.

## The queue, the steer, and the failure

The queue and the steer are two different things, and neither of them
carries a failure.

**The queue** holds work that is waiting. A prompt the user sent sits in
the queue until a worker runs it, and the queue runs its work in order:
the highest priority first, then the order it was sent in. **The steer**
sends input to the turn that is running right now. Neither one is a place
for an error.

A **failed** prompt is therefore not queue work. Its error belongs to the
conversation, where the user sees it and can answer it, and the queue
never lists it. A failure **pauses** the queue: the prompts that are
waiting stay exactly where they are, and the pause says what happened. The
user decides when the work behind it runs.

A prompt that was **interrupted** - the browser closed, the worker died,
the service restarted - pauses the queue the same way, with the waiting
prompts intact, so the user can come back to the conversation and resume.

**Resume does not start the queue.** It first checks what happened to the
last prompt the queue sent:

* the prompt finished, so the queue runs again and the next waiting
  prompt goes out in order;
* the prompt was interrupted, so the queue stays paused and says why, and
  the waiting prompts are kept.

## The command line

DeepSpace is drivable from a terminal, so the same agent work can be run
from a script or a terminal.

```
python -m app.deepspace.cli chats                 # the conversations
python -m app.deepspace.cli chat <id>            # one conversation
python -m app.deepspace.cli run <id> "the prompt" # send a prompt, print the answer
python -m app.deepspace.cli sandbox               # the sandbox status
python -m app.deepspace.cli plugins               # the plugin catalogue
python -m app.deepspace.cli presets               # the agent presets
python -m app.deepspace.cli terminals <id>        # the live terminal sessions
python -m app.deepspace.cli plan <id>             # the conversation's plan
```

`--json` prints machine-readable output instead of text. The exit code is
0 on success, 1 when the API reports an error, and 2 on a usage error.

The command line is a client of the same HTTP API the app uses, so it
needs the API's address and a bearer token: `AKS_API_URL` (default
`http://127.0.0.1:8000`) and `AKS_API_TOKEN`.

## The tool pipeline

Every tool call traverses three stages, whether the tool is native, a
connected MCP tool, or one of the package's own.

`pre`
    Decides whether the call may run. A participant may allow it, deny it
    with a reason, or cancel it. The product's own allowlist is the first
    participant.

`around`
    Wraps the body, so a participant can add a timeout, a retry, or a metric
    without owning the call.

`post`
    Decides what the model sees. A participant may accept the result, replace
    it, or block it. The product's own policy is the last participant, so a
    deployment's rules sit between the product's rules and the tool body.

A participant that raises is contained: the pipeline reports the failure and
continues, so one broken rule cannot fail every tool call.

Independent read calls in one provider round run at the same time, and
`deepspace_max_concurrent_tools` bounds how many run at once. A write call
is an ordering barrier: the calls before it complete before it starts, and
it completes before the calls after it start. That keeps the order of
state-changing work exact while the independent reads overlap.

A native write tool can also pause for a human decision. With
`deepspace_approval_mode=ask`, the pre stage marks the destructive tools
(`write`, `edit`, `delete`, the `fs_*` write tools, `bash`, `pwsh`, and
`sandbox_execute`) as requiring approval. The turn records the pending
approval in the run checkpoint, emits `permission_request` and
`approval_request`, and resumes from the stored decision. `never` keeps the
unattended behavior and is the default.

## The typed session log

`deepspace_run_events` stores the opaque SSE frames a browser replays.
`deepspace_session_events` stores the same run as structured records: turn
boundaries, step boundaries, assistant messages, tool calls, tool results,
delegation records, and plan-mode records. A step is one model round, so a
turn that used four tools records four steps.

The typed log is additive. A deployment that does not write typed events loses
nothing but the projections. The projections in
`backend/app/deepspace/services/session_projections.py` answer three
questions without parsing frames: which turn is open, which tools ran, and
what the delegation tree looks like.

`SESSION_FORMAT_VERSION` in `backend/app/deepspace/services/session_events.py`
records the record format. A structural change to the event names or the
payload shape bumps it.

## The compaction pipeline

A request passes through four stages before it reaches the model.

1. `elide_stale_messages` replaces the older completed messages with one
   marker. It keeps every system message, the opening user request, the last
   `deepspace_compaction_keep_recent_messages` messages, and complete tool
   exchanges, so a kept tool result never loses its call.
2. `prune_tool_results` shortens a tool result above
   `deepspace_tool_result_prune_threshold` to an excerpt plus a reference. The
   full result stays retrievable through `get_tool_result`, so nothing is
   lost, only deferred.
3. `offload_images` removes the image blocks above the request's image
   budget, keeping the newest because they are the ones the current turn is
   about.
4. The existing context-window fit runs, which trims ordinary prior turns
   and protects the active request, tool pairs, and a resumed turn's task.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `deepspace_workspace_root` | `<local_storage_path>/workspaces` | The base of the per-tenant project workspace. |
| `deepspace_shell_enabled` | `true` | Whether the local shell tools are offered. |
| `deepspace_permission_mode` | `workspace-write` | How much of the host a shell command may reach. |
| `deepspace_shell_timeout_seconds` | `120` | The wall-clock budget for one shell command. |
| `deepspace_shell_max_output_bytes` | `1000000` | The largest combined output one shell result may carry. |
| `deepspace_sandbox_policy` | `workspace-write` | The shell containment mode; overrides `deepspace_permission_mode` when set. |
| `deepspace_sandbox_backend` | `auto` | The operating-system cage: `auto`, `bwrap`, `landlock`, `sandbox-exec`, `windows-acl`, `docker`, or `none`. |
| `deepspace_loop_guard_enabled` | `true` | Whether the loop guard stops a repeating agent. |
| `deepspace_loop_guard_max_tool_calls` | `64` | The tool calls one turn may make. |
| `deepspace_loop_guard_max_identical_calls` | `4` | The same call, repeated, before the turn is stopped. |
| `deepspace_loop_guard_max_repeated_arguments` | `6` | The same arguments, under any tool, before the turn is stopped. |
| `deepspace_loop_guard_max_turn_seconds` | `900` | The wall-clock budget of one turn. |
| `deepspace_plugins_enabled` | `true` | Whether the plugin system is offered. |
| `deepspace_plugin_directory` | *(empty)* | The deployment's own plugin directory. |
| `deepspace_ssh_key_directory` | *(empty)* | The directory the deployment keeps its SSH private keys in. |
| `deepspace_ssh_runtime_enabled` | `true` | Whether the remote file, terminal and streaming tools are offered. |
| `deepspace_model_selection_mode` | `auto` | Whether DeepSpace chooses the model (`auto`) or the user does (`manual`). |
| `deepspace_model_auto_choice_enabled` | `true` | Whether the automatic choice is available at all. |
| `deepspace_hook_claude_dialect` | `true` | Whether the Claude Code and Codex hook dialect is accepted. |
| `deepspace_hook_audit_enabled` | `true` | Whether every hook invocation is written to the session log. |
| `deepspace_plugin_code_enabled` | `true` | Whether an extension may run code. The code runs in its own process, inside the cage, so the switch is a policy choice and not the only thing between an extension and the server. |
| `deepspace_agent_team_enabled` | `true` | Whether named teammates are offered. |
| `deepspace_agent_team_max_members` | `8` | The teammates one conversation may hold. |
| `deepspace_hook_manifest` | *(empty)* | The file or directory the deployment's tool hooks live in. |
| `deepspace_tool_timeout_seconds` | `120` | The deadline every native tool call runs under. |
| `deepspace_shell_capture_bytes` | `8000000` | The largest shell output that is kept; past it the output is spilled. |
| `deepspace_terminal_enabled` | `true` | Whether persistent terminal sessions are offered. |
| `deepspace_terminal_max_sessions_per_conversation` | `4` | The terminal sessions one conversation may hold. |
| `deepspace_terminal_max_sessions` | `32` | The terminal sessions the deployment may hold. |
| `deepspace_terminal_idle_timeout_seconds` | `900` | How long a terminal session may sit idle. |
| `deepspace_default_preset` | `standard` | The agent preset a conversation starts in. |
| `deepspace_sandbox_network` | `true` | Whether a sandboxed command keeps network access. |
| `deepspace_sandbox_image` | empty | The container image the Docker backend runs; Docker is skipped when empty. |
| `deepspace_sandbox_required` | `false` | Refuse a shell command when no sandbox backend is available. |
| `deepspace_approval_mode` | `never` | `ask` pauses a destructive native tool for a human decision; `never` runs it unattended. |
| `deepspace_subagent_max_depth` | `3` | The number of nested delegations a root agent may start. |
| `deepspace_subagent_max_children` | `16` | The number of children one conversation may hold at once. |
| `deepspace_max_concurrent_tools` | `8` | The largest number of tool calls one provider round may run at the same time. |
| `deepspace_tool_result_prune_threshold` | `8192` | The character ceiling above which a stored tool result is pruned from the request. |
| `deepspace_spill_threshold_chars` | `24000` | The character ceiling above which a tool result is spilled to durable storage. |
| `deepspace_spill_max_chars` | `2000000` | The largest result one spill accepts. |
| `deepspace_compaction_keep_recent_messages` | `24` | The number of recent messages the basic compaction stage keeps. |
| `deepspace_ptc_enabled` | `true` | Whether the model may run a program that calls tools. |
| `deepspace_ptc_timeout_seconds` | `60` | The wall-clock budget for one program. |
| `deepspace_ptc_max_tool_calls` | `24` | The largest number of host tool calls one program may make. |
| `deepspace_plan_mode_enabled` | `true` | Whether the plan mode is offered. |
| `deepspace_acp_enabled` | `false` | Whether the Agent Client Protocol bridge is offered. |
