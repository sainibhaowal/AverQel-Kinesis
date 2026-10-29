# 09. The tool catalogue

The native registry currently holds 133 tools. Each tool is declared once in
the module that owns its behaviour, and each tool returns a JSON result to the
model. This page groups the documented tools by subject, with the mode each
one runs under and the parameters it accepts. The runtime registry is the
authoritative inventory when a newer tool is added or a deployment narrows the
available set.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [The two modes](#the-two-modes)
  - [Files](#files)
  - [Shell and terminals](#shell-and-terminals)
  - [Remote runtime](#remote-runtime)
  - [Web](#web)
  - [Plans and goals](#plans-and-goals)
  - [Agents and teams](#agents-and-teams)
  - [Jobs and schedules](#jobs-and-schedules)
  - [Skills and workflows](#skills-and-workflows)
  - [Session log](#session-log)
  - [MCP](#mcp)
  - [Extensions and plugins](#extensions-and-plugins)
  - [Code and type graphs](#code-and-type-graphs)
  - [Programs, results, and inspection](#programs-results-and-inspection)
- [What to watch for](#what-to-watch-for)

## Use case

Read this page when you need to know what the agent can reach, or when you want
to write a hook, a plugin, or an extension that names a tool. The model does not
receive this list at once: it sees a compact catalogue and loads the schema of
one tool at a time (see "Dynamic tool discovery"). This page explains the
stable capability groups behind that step. The runtime registry and its
discovery report are the source of truth for the complete current set; this
page is not an exhaustive hand-maintained list of every registered name.

## Why it matters

- The mode of a tool decides how the runtime runs it. A `read` tool may run
  beside other read tools. A `write` tool runs under a write lock, one at a
  time.
- The parameters of a tool are checked before the tool runs. A call with an
  unknown parameter is refused, and so is a call that leaves out a required
  one.
- A deployment can withhold a tool. A tool that is switched off never appears
  in the catalogue the model sees, so the model never discovers it and is
  never refused after using it.

## How it works

### The two modes

| Mode | What the runtime does |
|---|---|
| `read` | The call is safe, so it may run beside other read calls, up to `deepspace_max_concurrent_tools` at once. |
| `write` | The call changes something, so it runs under the write lock, one at a time. |

Every call also runs under a deadline. The deadline is the tool's own when it
declares one, and `deepspace_tool_timeout_seconds` otherwise. A call that
overruns fails with the code `TOOL_TIMEOUT`.

### Files

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `fs_read` | read | Reads a text file from the project workspace and returns the content with line numbers. Use `offset` and `limit` for a large file. | `limit`, `offset`, `path` |
| `fs_write` | write | Creates or updates a text file. `mode` `create` fails when the file exists, `overwrite` replaces it, `append` adds to it. | `content`, `mode`, `path` |
| `fs_edit` | write | Replaces the whole content of a file, appends to it, renames it, or moves it inside the workspace. `expected_version` refuses the edit when the file changed since the last read. | `content`, `expected_version`, `new_path`, `operation`, `path` |
| `fs_str_replace` | write | Replaces one exact string inside a file. The match must be unique unless `replace_all` is true. `expected_version` refuses the replacement when the file changed since the last read. | `expected_version`, `new_string`, `old_string`, `path`, `replace_all` |
| `fs_delete` | write | Deletes a file. A directory needs `recursive` true. | `path`, `recursive` |
| `fs_grep` | read | Searches the workspace for a regular expression. `output_mode` `files_with_matches` lists the files, `count` gives per-file counts, `content` gives the matching lines. | `case_sensitive`, `glob`, `max_results`, `output_mode`, `path`, `pattern` |
| `fs_glob` | read | Lists workspace paths by name pattern, such as `**/*.ts`. | `max_results`, `path`, `pattern` |
| `workspace_diff` | read | Shows what changed in the workspace, as a unified diff of the changed files together with their paths. | `max_bytes`, `path` |
| `read_image` | read | Reads a PNG, JPEG, WebP, or GIF image from the workspace and returns it to a vision-capable model. | `max_bytes`, `path` |
| `present` | write | Presents one to four workspace-relative files as the deliverables of the turn. A path outside the workspace, or a file that does not exist, refuses the whole call. | `paths` |

### Shell and terminals

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `bash` | write | Runs a shell command in the project workspace and returns its exit code and output. Use it for builds, tests, and version control. | `command`, `cwd`, `timeout_seconds` |
| `pwsh` | write | Runs a PowerShell command in the project workspace and returns its exit code and output. It is offered on Windows deployments. | `command`, `cwd`, `timeout_seconds` |
| `terminal_open` | write | Opens a persistent terminal session and returns its identifier. Pass a command to run at startup, or omit it for an interactive shell. | `command`, `cwd`, `shell` |
| `terminal_read` | read | Reads the output a terminal session has produced so far. The output is buffered and bounded, and a read returns the oldest pending bytes. | `max_bytes`, `session_id` |
| `terminal_write` | write | Sends input to a running terminal session, such as a command to run or an answer to a prompt. The result carries a `wait_reason`. | `background`, `data`, `session_id`, `wait_seconds` |
| `terminal_signal` | write | Sends a signal to the foreground process group of a running terminal session, such as `SIGINT` or `SIGTERM`. | `session_id`, `signal` |
| `terminal_close` | write | Closes a terminal session and stops the process it runs. | `session_id` |
| `terminal_list` | read | Lists the conversation's live terminal sessions. | none |
| `terminal_job` | read | Reads the outcome of a background terminal write, such as the wait reason it settled with. | `job_id` |

### Remote runtime

These tools reach a remote host the deployment has reviewed. They are offered
only when `deepspace_ssh_runtime_enabled` is on.

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `ssh_host_list` | read | Lists the reviewed remote hosts this conversation may reach. | none |
| `ssh_run` | write | Runs a command on a reviewed remote host over SSH. The host key is checked and the command runs in the host's reviewed directory. | `command`, `host`, `stream`, `wait_seconds` |
| `ssh_fs_read` | read | Reads a text file on a reviewed remote host. A path that leaves the reviewed directory is refused. | `host`, `path` |
| `ssh_fs_write` | write | Writes a text file on a reviewed remote host. The file is written to a temporary name and renamed over the target, so a reader never sees half a file. | `content`, `host`, `path` |
| `ssh_fs_list` | read | Lists a directory on a reviewed remote host, with one entry per file carrying its name, its kind, and its size in bytes. | `host`, `path` |
| `ssh_fs_delete` | write | Removes a file from a reviewed remote host. | `host`, `path` |
| `ssh_terminal_open` | write | Opens a persistent terminal session on a reviewed remote host and returns its identifier. | `host` |
| `ssh_terminal_read` | read | Reads the output a remote terminal session has produced so far. | `max_bytes`, `session_id` |
| `ssh_terminal_write` | write | Sends input to a running remote terminal session. | `data`, `session_id` |
| `ssh_terminal_signal` | write | Sends a signal to a remote terminal session. `SIGINT`, `SIGQUIT`, and `SIGTSTP` reach the remote foreground process group. `SIGTERM`, `SIGHUP`, and `SIGKILL` end the connection and with it the session. | `session_id`, `signal` |
| `ssh_terminal_close` | write | Closes a remote terminal session and ends the connection it holds. | `session_id` |
| `ssh_stream_read` | read | Reads the output a streamed remote command has produced so far. The command keeps running. | `max_bytes`, `session_id`, `wait_seconds` |
| `ssh_stream_close` | write | Closes a streamed remote command and stops the command on the host. | `session_id` |

### Web

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `web_fetch` | read | Fetches a public HTTPS URL. It returns the page text for documents and the image metadata for pictures, so one tool covers both. | `allowed_domains`, `mode`, `url` |

### Plans and goals

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `plan_read` | read | Reads the conversation's current plan and its review state, including the user's feedback when they asked for changes. | none |
| `plan_submit` | write | Presents a finished plan for the user to review. The plan is Markdown that starts with a heading, names the steps in order, and states what each step will do and how it will be verified. The user approves it, asks for changes, or rejects it. | `plan`, `title` |
| `goal_read` | read | Reads the goals of the conversation. A goal is the standing objective of the conversation, and it survives a new turn. | `name` |
| `goal_set` | write | Creates, updates, or finishes a goal for the conversation. | `content`, `name`, `note`, `status` |

### Agents and teams

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `subagent` | write | Delegates a self-contained task to a child agent and receives its answer. The child works on its own context. Give it everything it needs in the task text. | `description`, `mode`, `model`, `persona`, `provider`, `task`, `tool_allow`, `tool_deny` |
| `subagent_fork` | write | Delegates a task to a child agent that inherits the conversation's recent history. The child answers once and ends. | `description`, `history_messages`, `model`, `persona`, `provider`, `task`, `tool_allow`, `tool_deny` |
| `send_message` | write | Sends a message to a continuable child agent by its id. The message is queued and the child runs it on its next turn. The call does not return the child's answer. | `agent_id`, `message` |
| `list_agents` | read | Lists a compact child-agent roster with stable ids, status, depth, and result availability. | `include_completed` |
| `get_agent` | read | Reads one child agent's detailed status and result by id. | `agent_id` |
| `list_subagent_models` | read | Lists the model routes a delegated child agent may run under, with the models each route offers. Use it to choose the provider and model you give a child. | none |
| `interrupt_agent` | write | Requests cancellation of a child agent's current turn. Only the current turn stops; queued messages stay queued. | `agent_id` |
| `wait_agent` | read | Waits until a teammate's state, a message, or a task on the shared board changes, and reads what changed. | `timeout_seconds` |
| `spawn_teammate` | write | Creates a named teammate that works the same task. The teammate is durable: it survives a restart and keeps the messages queued for it. | `model`, `name`, `persona`, `provider`, `tool_filter` |
| `team_send_message` | write | Sends a message to a named teammate. It reaches a running teammate at once and waits in the queue for one that is not running. | `body`, `to` |
| `list_teammates` | read | Compatibility detail view of named teammates and their last reported results. | none |
| `team_list_members` | read | Lists the compact canonical named-teammate roster and durable revision. | none |
| `team_get_member` | read | Reads one named teammate's detailed status and result by name. | `name` |
| `team_task_create` | write | Creates a task on the shared team board. Every teammate of the conversation works from the same board. | `body`, `depends_on`, `title` |
| `team_task_list` | read | Lists the tasks on the shared team board with their status, their owner, their dependencies, and their revision. | none |
| `team_task_update` | write | Updates a task on the shared team board. An update with an older revision is refused, so read the task again before you apply the change. | `body`, `owner`, `revision`, `status`, `task_id` |

### Jobs and schedules

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `job_list` | read | Lists the queued work of this conversation. | `limit`, `status` |
| `job_status` | read | Reads the status and the detail of one background job. | `job_id` |
| `job_output` | read | Reads the output a background job produced: the text it wrote and the results its tools returned. `wait_seconds` waits for a running job to finish. | `job_id`, `max_bytes`, `wait_seconds` |
| `job_kill` | write | Cancels one queued or running background job. | `job_id` |
| `schedule_create` | write | Creates a recurring follow-up for the conversation. The prompt is sent back into the conversation every `interval_minutes` minutes, so it has to stand on its own. | `interval_minutes`, `name`, `prompt` |
| `schedule_list` | read | Lists the recurring follow-ups of the conversation with their interval, their state, and the time they run next. | none |
| `schedule_update` | write | Changes a recurring follow-up: its name, its prompt, its interval, or whether it is active or paused. A paused schedule keeps its place but does not run. | `interval_minutes`, `name`, `prompt`, `schedule_id`, `status` |
| `schedule_delete` | write | Removes one recurring follow-up. The follow-up stops running and is no longer listed. | `schedule_id` |

### Skills and workflows

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `skill_list` | read | Lists the skills installed in the project workspace. | `query` |
| `skill_load` | read | Reads one skill's instructions. Read a skill before you follow it. | `name` |
| `workflow_run` | write | Runs a workflow installed in the project workspace under `.averqel/workflows`. Each step runs as its own agent. | `input`, `name` |
| `ralph_run` | write | Runs a script from the project workspace over repeated fresh agents until one reports `RALPH_DONE` or the round budget is spent. | `max_rounds`, `script`, `task` |

### Session log

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `session_search` | read | Searches this account's past conversations for a text, such as a file name, a tool name, or a phrase from an earlier answer. It reports the conversations that mention the text, newest first, with the event that matched. | `limit`, `query` |
| `session_trace` | read | Reads the timeline of one conversation: its turns, its steps, and its tool calls in the order they happened. | `conversation_id` |
| `session_event_read` | read | Reads one event of one conversation in full, with the events before and after it. | `conversation_id`, `seq` |
| `session_event_search` | read | Searches the events of one conversation for a text. The events come back in the order the conversation recorded them. | `conversation_id`, `limit`, `query` |
| `session_event_trace` | read | Reads the lineage of one event: the turn it belongs to, the step it belongs to, and the events of that step. | `conversation_id`, `seq` |

### MCP

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `list_mcp_resources` | read | Lists the resources the connected MCP servers publish, with the URI to read, the resource name, and the media type when the server publishes one. | none |
| `list_mcp_resource_templates` | read | Lists the resource URI templates the connected MCP servers publish. A template such as `file://{path}` describes a family of resource URIs. | none |
| `read_mcp_resource` | read | Reads one resource published by a connected MCP server. | `uri` |

### Extensions and plugins

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `extension_list` | read | Lists the extensions the conversation runs with. An extension adds guidance to the system prompt, registers an alias for a tool, or restricts which tools the agent may use. | none |
| `extension_write` | write | Installs or changes one extension. A restriction can only narrow what the agent may do; it cannot widen the deployment's own policy. | `aliases`, `approval_tools`, `deny_tools`, `description`, `guidance`, `name` |
| `extension_remove` | write | Removes one extension from the conversation. | `name` |
| `plugin_install` | write | Installs a plugin into the conversation's workspace, so the plugin's own tools become callable from the next turn. The deployment's policy always wins. | `description`, `enabled_by_default`, `guidance`, `handler`, `handler_kind`, `handler_module`, `handler_timeout_seconds`, `name`, `tools` |
| `plugin_manager` | write | Lists the plugins the deployment ships, and switches one on or off for this conversation. A plugin takes effect on the next turn. | `action`, `plugin_id` |

### Code and type graphs

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `type_graph` | read | Reads the type graph of the workspace: the classes, functions, methods, interfaces, and types it declares, and the references between them. Give a name to see where it is defined and what refers to it. | `name`, `path` |
| `code_metrics` | read | Counts the files and the lines of code in the workspace, grouped by file extension. | `path` |
| `load_workspace_dependencies` | read | Reports where the libraries and binaries the deployment ships live. The result names the installed Python packages with the absolute path of each one, and the paths of the node, pnpm, and python binaries. A package that is not installed is left out, and a binary this host does not carry is reported as null. | none |

### Programs, results, and inspection

| Tool | Mode | What it does | Parameters |
|---|---|---|---|
| `run_code` | write | Runs one Python program that calls the tools as ordinary function calls. The program receives a `tool` function: `tool('web_search', {'query': '...'})` returns that tool's result. The program runs in a confined sandbox with no network, so every piece of data it needs must come from a tool call. | `code`, `timeout_seconds`, `tools` |
| `spill_read` | read | Reads a spilled tool result. A result too large for the context window is offloaded to durable storage and reported with a `spill_ref`. | `max_characters`, `offset`, `spill_ref` |
| `hook_run` | read | Asks the deployment's hooks about a tool call before the agent makes it. The hooks allow the call, block it with a reason, or ask about it. | `arguments`, `event`, `tool_name` |
| `averqel_inspect_list` | read | Lists the capabilities this runtime offers: the plugin system, the native tool registry, the sandbox backends, the provider routes, the terminal manager, and the schedule service. | none |
| `averqel_inspect_query` | read | Reads one capability of this runtime in detail: the tools it owns and the mode each one runs under, the settings that configure it, and its current state. | `capability` |
| `runtime_discover` | read | Reports what this runtime can do: the tools the agent may call, the plugins, the model routes a child may run under, and the installed extensions. The report runs nothing. | `kind` |

## What to watch for

- The catalogue is not a promise that a tool is available. The deployment can
  switch one off, the conversation's preset can withhold it, and a connected
  service may be missing.
- A tool that fails reports a category and, where it has one, a stable code.
  The codes in use are `TOOL_TIMEOUT`, `FS_STALE_VERSION`, `FS_AMBIGUOUS_EDIT`,
  and `SANDBOX_UNAVAILABLE`.
- A tool that takes a path refuses a path that leaves the workspace. A refusal
  is a result the model can read and act on, not a crash.
- The `code_metrics` tool is a plugin tool, not a built-in one. It is listed
  here because the deployment ships it by default.
- The provider boundary may expose fewer names than the registry holds. It
  registers the general-purpose provider tools `discover_tools`,
  `get_tool_result`, `web_search`, `url_read`, `image_read`, `read`, `find`,
  `write`, `edit`, `delete`, `analyze`, `sandbox_execute`, `artifact_create`,
  `todo_write`, and `ask_user`. The names the provider sees are the ones the
  discovery step releases, and they change with the deployment's settings and
  the conversation's preset.
- This page describes the stable native groups in this release. A tool added
  later appears in the runtime's own report, which the agent can read through
  `runtime_discover` or `averqel_inspect_query`.
