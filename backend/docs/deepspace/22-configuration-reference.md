# 22. Configuration reference

This page covers the core `deepspace_*` settings used across the manual, with
their type, default, and effect. The complete settings model is declared in
`backend/app/core/config.py`; feature-specific settings are documented with
their owning chapters, including [telemetry](28-session-telemetry.md),
[invariants](29-runtime-invariants.md), [plugin HMR](30-plugin-hmr.md),
[permission presets](32-permission-presets.md), [conversation forks](35-conversation-fork.md),
[feedback](36-session-feedback.md), [headless runs](37-headless-profile.md),
[workflows](38-workflow-orchestration.md), and [goals](39-goal-driven-conversations.md).

## Use case

- An operator has to change one limit and needs to know its exact name,
 type, and default.
- A developer has to know which settings the runtime reads and which are
 only declared.
- Someone reviewing a deployment has to check the security settings first:
 the sandbox, the approval mode, the extension code switch, and the path
 root.

## Why it matters

- The defaults are a safe starting point, not a safe production setting.
 `deepspace_sandbox_network` is on by default, and `deepspace_approval_mode`
 is `never`, so a deployment that changes nothing runs unattended commands
 with network access.
- A setting that does not exist is silently ignored. The settings class
 ignores an environment variable it does not declare, so a name that is
 read but not declared has no effect at all.
- A mistyped value is refused at load, not at use. A value outside the
 allowed range stops the process before a request is served.

## How it works

### How the settings are read

The settings class is `Settings` in `backend/app/core/config.py`. It is a
`pydantic-settings` model with:

- The environment prefix `AKS_`, so `deepspace_sandbox_backend` is set with
 `AKS_DEEPSPACE_SANDBOX_BACKEND`.
- The environment variable names case-insensitive.
- A `.env` file in the working directory.
- Extra environment variables ignored, so an unknown variable does not stop
 the process.

A value outside a setting's allowed range raises a validation error when
the settings are first read, so a bad value stops the start rather than
surfacing later.

### Sandbox and containment

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_sandbox_backend` | `auto`, `bwrap`, `landlock`, `sandbox-exec`, `docker`, `windows`, `none` | `auto` | Which operating-system cage runs the shell commands and the extension code. `auto` picks the best one the host offers. `none` keeps inspection only. |
| `deepspace_sandbox_policy` | `read-only`, `workspace-write`, `danger-full-access` | `workspace-write` | The containment the deployment starts a conversation in. A preset may override it. |
| `deepspace_sandbox_network` | `bool` | `true` | Whether a command inside the cage keeps network access. Turn it off for a deployment that runs untrusted code. |
| `deepspace_sandbox_required` | `bool` | `false` | Whether a command is refused when no cage is available. Off, the command runs uncontained. On, it fails with the code `SANDBOX_UNAVAILABLE`. |
| `deepspace_sandbox_image` | `str` or `null` | `null` | The container image the Docker backend runs. Empty, the Docker backend is skipped, so no image is pulled without an explicit choice. |
| `deepspace_sandbox_enabled` | `bool` | `true` | Whether the separate code and data executor is used at all. The development default points at the local executor; production deployments must set their own endpoint and token. It is never run inside the API or the worker. |
| `deepspace_sandbox_url` | `str` or `null` | `http://127.0.0.1:3020` | The address of the local development executor. The executor is refused when this is empty. Production deployments must replace this value. |
| `deepspace_sandbox_token` | `str` or `null` | `local-sandbox-e2e-token` | The local development bearer token the executor expects. Production deployments must replace this value and keep it out of source control. |
| `deepspace_sandbox_timeout_seconds` | `int` (1-30) | `20` | The budget one call to that executor may take. |

### Shell

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_shell_enabled` | `bool` | `true` | Whether the `bash` and `pwsh` tools are offered at all. |
| `deepspace_permission_mode` | `read-only`, `workspace-write`, `danger-full-access` | `workspace-write` | How much of the host the shell commands may reach. `read-only` refuses commands that plainly change state, and `workspace-write` starts every command inside the workspace. |
| `deepspace_shell_timeout_seconds` | `int` (1-900) | `120` | The budget one shell command may take. |
| `deepspace_shell_max_output_bytes` | `int` (1 024-20 000 000) | `1000000` | The largest command output kept in the tool result. |
| `deepspace_shell_capture_bytes` | `int` (65 536-64 000 000) | `8000000` | The largest command output captured before it is spilled to durable storage and read back in pages. |

### Terminal

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_terminal_enabled` | `bool` | `true` | Reported in the runtime status and by `runtime_discover`. It does not gate the terminal tools themselves. |
| `deepspace_terminal_max_sessions_per_conversation` | `int` (1-32) | `4` | How many live terminal sessions one conversation may hold. |
| `deepspace_terminal_max_sessions` | `int` (1-256) | `32` | How many live terminal sessions the deployment may hold. |
| `deepspace_terminal_idle_timeout_seconds` | `float` (30-86 400) | `900.0` | How long an unused session stays alive before it is closed. |
| `deepspace_terminal_max_output_bytes` | `int` (1 024-20 000 000) | `262144` | The number of output bytes one session keeps. A session that writes more drops its oldest bytes. |

### Browser

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_browser_enabled` | `bool` | `false` | Whether the deployment runs a browser runtime. The local single-machine deployment turns it on; every other deployment makes the choice itself. |
| `deepspace_browser_executable` | `str` | `""` | The path of the Chromium binary the runtime starts. An empty value makes the runtime look for a browser in the usual places and report clearly when the host has none. |
| `deepspace_browser_idle_timeout_seconds` | `float` (30-86 400) | `900.0` | How long an unused browser session stays alive before it is closed. |

### Tools and results

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_max_concurrent_tools` | `int` (1-32) | `8` | How many tool calls one provider round may run at the same time. Independent read calls share the budget, and write calls stay serialised behind the write lock. |
| `deepspace_tool_timeout_seconds` | `float` (greater than 0, at most 3 600) | `120.0` | The wall-clock budget one tool call may take. A tool that declares its own deadline uses that one instead. An overrun fails with the code `TOOL_TIMEOUT`. |
| `deepspace_tool_result_prune_threshold` | `int` (1 024-200 000) | `8192` | The character count above which a stored tool result is pruned from the request history and replaced by an excerpt plus a retrievable reference. |
| `deepspace_spill_threshold_chars` | `int` (2 000-500 000) | `24000` | The character count above which a tool result is spilled to durable, content-addressed storage. The model receives a preview and a reference it can read back with `spill_read`. |
| `deepspace_spill_max_chars` | `int` (10 000-20 000 000) | `2000000` | The largest spilled result the spill store accepts, in characters. |

### Agents and presets

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_default_preset` | `str` | `standard` | The agent preset a new conversation starts in. The catalogue holds `standard`, `ptc`, `minimal`, `creator`, and `custom`. |
| `deepspace_subagent_max_depth` | `int` (0-8) | `3` | How many nested delegations a root agent may start. Depth 3 lets a root delegate to a child that delegates once more. |
| `deepspace_subagent_max_children` | `int` (1-64) | `16` | How many children one conversation may hold at once. Every level obeys the same cap. |
| `deepspace_agent_team_enabled` | `bool` | `true` | Whether named teammates are available. |
| `deepspace_agent_team_max_members` | `int` (2-32) | `8` | How many named teammates one conversation may hold. |
| `deepspace_plan_mode_enabled` | `bool` | `true` | Whether the plan mode and its plan submission tool are offered. |
| `deepspace_approval_mode` | `ask`, `never` | `never` | Whether a destructive call pauses for a human decision. `ask` records a pending approval and resumes the turn after the user answers. `never` keeps unattended execution. |
| `deepspace_ptc_enabled` | `bool` | `true` | Whether programmatic tool calling is available at all. |
| `deepspace_ptc_timeout_seconds` | `int` (5-300) | `60` | The budget one programmatic program may take. |
| `deepspace_ptc_max_tool_calls` | `int` (1-128) | `24` | How many tool calls one program may make. |
| `deepspace_reasoning_effort` | `low`, `medium`, `high`, or `null` | `null` | The reasoning effort a conversation asks for when its preset names none. A value the selected model does not support falls back to the provider's own default. |
| `deepspace_default_reasoning_effort` | `low`, `medium`, `high`, or `null` | `null` | The same setting under a second name. It is read when `deepspace_reasoning_effort` names none, so a deployment may use either name. |

### The agent run

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_agent_max_runtime_seconds` | `int` (30-86 400) | `1800` | The wall-clock budget one agent turn may take. The runtime may extend the deadline a bounded number of times, so it is the length of one attempt. |
| `deepspace_agent_retained_steps` | `int` (100-10 000) | `10000` | The number of agent steps one run keeps. The oldest steps are pruned from the run, so it is the size of the replayable history. |

### Hooks

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_hook_manifest` | `str` | `""` | The manifest that names the deployment's hooks. Empty, no hook runs. |
| `deepspace_hook_claude_dialect` | `bool` | `true` | Whether the Claude Code and Codex answer format is accepted alongside DeepSpace's own answer, which is `allow`, `block`, or `ask`. |
| `deepspace_hook_audit_enabled` | `bool` | `true` | Whether every hook invocation writes the two events `hook/invoked` and `hook/result` to the typed log. |

### Plugins and extensions

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_plugins_enabled` | `bool` | `true` | Whether the plugin system is available to a conversation. |
| `deepspace_plugin_code_enabled` | `bool` | `true` | Whether an extension may run code. Off, extension code is refused entirely. |
| `deepspace_plugin_directory` | `str` | `""` | The deployment's own plugin directory, read in addition to the built-in plugins. |
| `deepspace_extensions_dir` | `str` or `null` | `null` | The directory that holds the extensions the deployment installed for every conversation. Empty, `<local_storage_path>/extensions` is used. |

### MCP

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_mcp_max_search_results` | `int` (1-20) | `5` | How many tools a catalogue search returns. |
| `deepspace_mcp_max_schema_chars` | `int` (1 000-20 000) | `6000` | The largest schema hint the broker returns. |
| `deepspace_mcp_max_result_chars` | `int` (2 000-100 000) | `12000` | The largest single tool result the broker returns. |
| `deepspace_mcp_max_calls_per_turn` | `int` (1-32) | `8` | How many MCP calls one turn may make. |
| `deepspace_mcp_max_discovery_calls_per_turn` | `int` (1-32) | `8` | How many discovery calls one turn may make. |
| `deepspace_mcp_max_result_chars_per_turn` | `int` (4 000-500 000) | `60000` | The total MCP result size one turn may carry. |
| `deepspace_mcp_max_argument_chars_per_call` | `int` (1 000-100 000) | `20000` | The largest argument payload one MCP call may carry. |

### Workspaces

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_workspace_root` | `str` or `null` | `null` | The base directory that holds every account's workspaces. Empty, `<local_storage_path>/workspaces` is used. A deployment with neither has no workspace, and the file and shell tools report that. |

### Research and web reading

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `searxng_base_url` | `str` | `http://127.0.0.1:8888` | The SearXNG JSON endpoint used by the built-in web-search candidate. Docker deployments override it with `http://searxng:8080`. |
| `searxng_local_runtime_enabled` | `bool` | `false` | Whether the standalone local entrypoint supervises a native SearXNG process. `run_backend.py` enables it for non-Docker launches. |
| `searxng_runtime_dir` | `str` or `null` | `null` | The directory for the local source checkout, virtual environment, settings, log, and PID marker. Empty, `~/.averqel-harness/searxng` is used. |
| `searxng_source_url` | `str` | official SearXNG Git URL | The source repository cloned for the local runtime. |
| `searxng_source_ref` | `str` | `master` | The Git branch or tag cloned for the local runtime. |
| `searxng_install_timeout_seconds` | `int` (60-3600) | `900` | The maximum time allowed for one native installation command. |
| `searxng_start_timeout_seconds` | `int` (10-300) | `90` | The maximum time allowed for the local HTTP endpoint to become ready. |
| `deepspace_research_enabled` | `bool` | `true` | Whether the web research path is available. |
| `deepspace_research_browser_enabled` | `bool` | `false` | Whether the isolated browser renderer is used to read a page that plain fetching cannot. It stays off until an isolated renderer is deployed and authenticated. |
| `deepspace_research_browser_url` | `str` or `null` | `null` | The address of that renderer. |
| `deepspace_research_browser_token` | `str` or `null` | `null` | The bearer token that renderer expects. |
| `deepspace_url_read_timeout_seconds` | `int` (5-30) | `15` | The budget one URL read may take. |
| `deepspace_url_read_max_bytes` | `int` (16 384-2 000 000) | `2000000` | The largest response one URL read may download. |
| `deepspace_url_read_max_chars` | `int` (4 000-200 000) | `48000` | The largest response one URL read may hand to the model. A large page cannot take the remaining context window in one tool result. |
| `deepspace_url_allowed_domains` | `list[str]` | `[]` | The domains a URL read may reach. Empty, any public host is allowed. A caller that names its own domains uses those instead. |

### Models

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_model_selection_mode` | `auto`, `manual` | `auto` | Whether the model is chosen for each turn or taken from the user's choice. |
| `deepspace_model_auto_choice_enabled` | `bool` | `true` | Whether the automatic choice is available at all. A conversation recorded as `auto` becomes `manual` when this is off. |
| `deepspace_provider_read_timeout_seconds` | `int` (15-300) | `300` | The budget an idle provider stream may take. It never limits the model's output token budget. |
| `deepspace_provider_prompt_caching_enabled` | `bool` | `true` | Whether provider-native, ephemeral prompt caching is used. Only paths an adapter implements explicitly are used, and no provider-specific field reaches a custom endpoint. |

### The rest

| Setting | Type | Default | What it changes |
|---|---|---|---|
| `deepspace_loop_guard_enabled` | `bool` | `true` | Whether a turn that repeats itself is stopped. |
| `deepspace_loop_guard_max_tool_calls` | `int` (1-1 000) | `64` | How many tool calls one turn may make. |
| `deepspace_loop_guard_max_identical_calls` | `int` (1-100) | `4` | How many times the same call with the same arguments may repeat. |
| `deepspace_loop_guard_max_repeated_arguments` | `int` (1-200) | `6` | How many times the same arguments may repeat across different calls. |
| `deepspace_loop_guard_max_turn_seconds` | `float` (30-7 200) | `900.0` | How long one turn may run. |
| `deepspace_compaction_keep_recent_messages` | `int` (4-200) | `24` | How many recent messages the basic compaction stage always keeps. Older complete exchanges are replaced by one marker. |
| `deepspace_ssh_runtime_enabled` | `bool` | `true` | Whether the remote runtime is available, so the whole tool surface can run on a reviewed remote host. |
| `deepspace_ssh_key_directory` | `str` | `""` | The directory that holds the private keys for the reviewed remote hosts. The database holds only the name of a key. |
| `deepspace_acp_enabled` | `bool` | `false` | Whether the Agent Client Protocol bridge is on, so a trusted programmatic client can drive a conversation over JSON-RPC. |
| `deepspace_proactive_daemon_enabled` | `bool` | `false` | Whether the proactive daemon runs. The daemon looks for the recurring schedules that are due and enqueues their runs, for a deployment that does not run the Celery beat scheduler. |
| `deepspace_proactive_daemon_interval_seconds` | `int` (1-3 600) | `60` | How long the daemon waits between two passes over the schedules. |

## What to watch for

- The defaults are not a production configuration. A production deployment
 that runs untrusted code needs `deepspace_sandbox_network=false`,
 `deepspace_sandbox_required=true`, and `deepspace_approval_mode=ask`.
- The settings a preset names win over the deployment's value for that
 conversation. `minimal` always runs read-only, and `ptc` always allows
 one level of delegation, whatever the deployment's numbers say.
- The web research path has no query, candidate, or fetch budget. The model
 drives the research itself with `web_search` and `url_read`, and the
 product does not run a second, hidden research pipeline, so there is
 nothing for such a limit to bound. The ceilings that do apply are
 `deepspace_url_read_max_bytes`, `deepspace_url_read_max_chars`, and the
 loop guard.
- `deepspace_terminal_enabled` is reported in the runtime status and by
 `runtime_discover`, and it does not switch the terminal tools off.
- `deepspace_sandbox_policy` and `deepspace_permission_mode` carry the same
 three values. The preset resolution reads the policy first and the
 permission mode second, so a deployment that sets only the permission mode
 still works.
- A value outside a setting's allowed range stops the process when the
 settings are first read. The message names the setting and the range.
- The `AKS_` prefix applies to every setting, including the ones this
 chapter lists. `AKS_DEEPSPACE_SPILL_THRESHOLD_CHARS=50000` sets
 `deepspace_spill_threshold_chars`.
