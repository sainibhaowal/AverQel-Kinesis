# 25. Glossary

Every term this manual uses, with a plain definition and the place the term
lives. The file or route is named so a reader can go and read the code
behind the word.

## Use case

- A reader who meets a word in another chapter and wants it explained in
  one line.
- A developer who knows the concept and wants to find the file that owns
  it.
- A reviewer checking that two chapters use a word the same way.

## Why it matters

- Several words in this product look like words from other products and
  mean something slightly different here. **Session** names two different
  things. **Mode** names three.
- A glossary that lists only the obvious words helps a reader less than one
  that says where each word lives.
- A term without a file behind it is a slogan. Naming the file keeps the
  term honest.

## How it works

### The conversation

- **Conversation** - one thread of turns, with its own identifier. The row
  lives in `app/deepspace/models/conversation.py`, and the routes are under
  `/api/v1/deepspace/chats`.
- **Turn** - one user message and everything the agent did in answer to it.
  The turn is written to the log as `turn/start` and `turn/end`, in
  `app/deepspace/services/session_events.py`.
- **Step** - one unit of work inside a turn, usually one tool call and its
  result. The log names it with `step/start` and `step/end`, in
  `app/deepspace/services/session_events.py`.
- **Tool call** - one request from the model for one tool, with its
  arguments. The log names it `tool/call` and `tool/result`, and the
  dispatcher lives in `app/deepspace/services/tool_pipeline.py`.
- **Trajectory** - the path a conversation took, rebuilt from the log
  alone. The rebuilding code is in `app/deepspace/services/replay_trace.py`
  and `app/deepspace/services/session_projections.py`.
- **Session log** - the typed record of everything that happened in a
  conversation. The store is `app/deepspace/services/session_events.py`,
  and the table is `deepspace_session_events`.
- **Compaction** - the stage that keeps a long conversation inside the
  model's context window by summarising the older exchanges. The code is
  `app/deepspace/services/compaction_pipeline.py`, and the setting is
  `deepspace_compaction_keep_recent_messages`.
- **Spill** - moving a large tool result out of the model's context into
  durable storage and leaving a short reference behind. The store is
  `app/deepspace/services/spill_store.py`, and the tool that reads a
  spilled result back is `spill_read`.
- **Prompt** - the text the user sent. The field in the interface is
  `frontend/app/dashboard/deepspace/_components/DeepSpaceComposer.tsx`.
- **Attachment** - a file sent with a message. The store is
  `app/deepspace/services/attachment_store.py`.
- **Deliverable** - a file the turn declares as its output. The code is
  `app/deepspace/services/deliverables.py`, and the tool is `present`.
- **Citation** - the source a web claim came from. The web tools are in
  `app/deepspace/services/tools/web.py`.
- **Library** - the account's file store, beside the workspace. The routes
  are under `/api/v1/deepspace/library/{conversation_id}/...`, in
  `app/deepspace/api/library.py`.
- **Memory** - the facts the account keeps between conversations. The
  routes are under `/api/v1/deepspace/chats/memory`, in
  `app/deepspace/api/chats.py`.
- **Task plan** - the persisted list of steps the agent is working
  through. The tool is `todo_write`, declared in
  `app/deepspace/services/tool_registry.py`, and the plan that a plan-mode
  turn produces is stored by `app/deepspace/services/plan_store.py`.

### How the agent runs

- **Mode** - a word used for three different things in this product. The
  **agent mode** says how the agent works. The **sandbox mode** says how
  much of the host the agent may reach. The **model selection mode** says
  who picks the model.
- **Preset** - a named bundle of settings a conversation starts with: the
  tools the agent may call, whether it may delegate, whether it may write
  a program, and which sandbox mode it starts in. The catalogue is
  `app/deepspace/services/presets.py`, and the routes are
  `GET /api/v1/deepspace/chats/presets` and
  `GET`/`PUT /api/v1/deepspace/chats/{conversation_id}/preset`.
- **Agent mode** - the preset a conversation runs in. The catalogue holds
  `standard`, `ptc`, `minimal`, `creator`, and `custom`. The menu is
  `frontend/app/dashboard/deepspace/_components/DeepSpaceAgentModeMenu.tsx`.
- **Programmatic tool calling** - the `ptc` mode, in which the agent
  writes one program that calls the native tools as ordinary function
  calls. The runtime is `app/deepspace/services/ptc_runtime.py`.
- **Plan mode** - the state in which the agent designs the work and the
  user approves the plan before anything runs. The service is
  `app/deepspace/services/plan_mode.py`, the tools are `plan_submit` and
  `plan_read` in `app/deepspace/services/tools/plan.py`, and the routes are
  `GET`/`PUT /api/v1/deepspace/chats/{conversation_id}/plan/mode`.
- **Approval** - the pause before a destructive call runs, with the user's
  decision recorded. The policy is
  `app/deepspace/services/runtime_policy.py`, and the route is
  `POST /api/v1/deepspace/chats/{conversation_id}/approvals/{approval_id}`.
- **Approval mode** - the deployment's choice between asking and never
  asking. The setting is `deepspace_approval_mode`.
- **Participant** - one rule that sits in a stage of the tool pipeline. The
  stages are `pre`, `around`, and `post`, in
  `app/deepspace/services/tool_pipeline.py`.
- **Read semaphore** - the limit on how many read calls run at the same
  time. It is `state.read_semaphore` in
  `app/deepspace/services/chat_service.py`.
- **Write lock** - the lock that keeps write calls one at a time. It is
  `state.write_lock` in `app/deepspace/services/chat_service.py`.
- **Tool batch** - the grouping of one round's calls into parallel read
  batches and serial write batches. The planner is
  `app/deepspace/services/execution_batches.py`.
- **Loop guard** - the check that stops a turn that repeats itself. The
  code is `app/deepspace/services/loop_guard.py`, and the settings are the
  `deepspace_loop_guard_*` names.
- **Reasoning effort** - how much the model is asked to think before it
  answers. The options follow the model, and the code is
  `frontend/app/dashboard/deepspace/_lib/reasoning-options.ts`.

### Working together

- **Delegation** - starting another agent to do a piece of the work. The
  tools are in `app/deepspace/services/tools/delegation.py`.
- **Child** - a delegated agent, with its own conversation, its own model,
  its own tools, and its own turn history. The code is
  `app/deepspace/services/subagent_runner.py`, and the row is
  `app/deepspace/models/subagent.py`.
- **One-shot child** - a child that runs once and stops.
- **Continuable child** - a child that can receive more messages later.
- **Depth** - how many nested delegations a root agent may start. The
  setting is `deepspace_subagent_max_depth`.
- **Teammate** - a continuable child with a name. The code is
  `app/deepspace/services/agent_team.py`, and the row is
  `app/deepspace/models/agent_team.py`.
- **Team board** - the shared list of tasks the teammates work on. The
  tools are `team_task_create`, `team_task_list`, and `team_task_update`,
  in `app/deepspace/services/tools/team.py`.
- **Team message** - a message sent to a named teammate. The tool is
  `team_send_message`, and the log names it `team/message`.
- **Job** - long work the product runs in the background. The tools are
  `job_list`, `job_status`, and `job_kill` in
  `app/deepspace/services/tools/jobs.py`, and `job_output` in
  `app/deepspace/services/tools/job_output.py`.
- **Schedule** - work the product runs later on a timetable. The routes
  are under `/api/v1/deepspace/schedules`, in
  `app/deepspace/api/schedules.py`, and the tools are
  `schedule_create`, `schedule_list`, `schedule_update`, and
  `schedule_delete` in `app/deepspace/services/tools/schedule.py`.
- **Queue** - the list of prompts waiting for a turn. The code is
  `app/deepspace/services/turn_queue.py`, and the row is
  `app/deepspace/models/queued_turn.py`. The routes are under
  `/api/v1/deepspace/chats/{conversation_id}/queue`.
- **Worker** - the Celery process that runs the subagent turns, the
  indexing, the Library work, and the schedules. The tasks are in
  `app/deepspace/workers/`, and the app is
  `app/platform/worker/celery_app.py`.
- **Task loop** - the repeated-fresh-agents run, the `ralph_run` tool. The
  code is `app/deepspace/services/task_loop.py`.
- **Workflow** - a named sequence of steps run as child agents. The tool is
  `workflow_run`, in `app/deepspace/services/tools/workflow.py`.

### The tools and their surroundings

- **Native tool** - a tool the product ships, declared once in the module
  that owns its behaviour under `app/deepspace/services/tools/`, and
  collected in `app/deepspace/services/tools/__init__.py`. The names, the
  schemas, the modes, and the handlers all come from that one
  declaration.
- **Tool discovery** - the way the model loads one tool's exact schema when
  it needs it. The tool is `discover_tools`, declared in
  `app/deepspace/services/tool_registry.py`.
- **Extension** - a declarative addition to a conversation: guidance for
  the system prompt, a tool alias, or a refused tool. It lives in the
  workspace under `.deepspace/extensions/`, and the code is
  `app/deepspace/services/extensions.py`.
- **Plugin** - an optional manifest that adds tools, guidance, or restrictions
  to a conversation. It may carry code, but the product's own runtime remains
  core rather than becoming a plugin. The code is
  `app/deepspace/services/plugins.py`, and the routes are `GET`/`PUT
  /api/v1/deepspace/chats/{conversation_id}/plugins`.
- **Core runtime** - the DeepSpace services that every conversation relies
  on, including the agent loop, session log, tool policy, sandbox, presets,
  workers, MCP bridge, and plugin registry. The settings inventory reports
  these separately from optional plugins.
- **Extension jail** - the separate process, inside the operating-system
  cage, that a plugin's code runs in. The runner is `run_python_handler`
  and `run_command_handler` in `app/deepspace/services/plugins.py`.
- **Hook** - an external command that watches a tool call and may refuse
  it. The code is `app/deepspace/services/hooks.py`, and the setting that
  names them is `deepspace_hook_manifest`.
- **Hook gate** - the place in the tool pipeline where the hooks are
  asked. The code is `app/deepspace/services/hook_gate.py`.
- **Cage** - the operating-system boundary a command runs inside. The
  code is `app/deepspace/services/sandbox_backend.py`.
- **Sandbox backend** - one way of building the cage: `bwrap`, `landlock`,
  `sandbox-exec`, `windows`, `docker`, or `none`. The setting is
  `deepspace_sandbox_backend`.
- **Sandbox mode** - how much of the host the agent may reach:
  `read-only`, `workspace-write`, or `danger-full-access`. The service is
  `app/deepspace/services/sandbox_mode.py`, and the routes are
  `GET`/`PUT /api/v1/deepspace/chats/{conversation_id}/sandbox-mode`.
- **Permission mode** - the deployment's own name for the same three
  values, read when a preset names none. The setting is
  `deepspace_permission_mode`.
- **Shell session** - a terminal that stays alive between tool calls. The
  code is `app/deepspace/services/terminals.py`, and the routes are under
  `/api/v1/deepspace/chats/{conversation_id}/terminals`.
- **Workspace** - the directory a conversation works in. The code is
  `app/deepspace/services/workspace_root.py` and
  `app/deepspace/services/workspaces.py`, and the routes are
  `GET`/`PUT /api/v1/deepspace/chats/{conversation_id}/workspace`.
- **Named workspace** - a workspace beside the account's default one, named
  by the user.
- **Remote runtime** - the tool surface running on a reviewed host over
  SSH. The code is `app/deepspace/services/ssh.py` and
  `app/deepspace/services/ssh_runtime.py`.
- **MCP** - Model Context Protocol, the way an external server publishes
  tools and resources. The bridge is
  `app/deepspace/services/mcp_bridge.py`.
- **Broker** - the part of the MCP bridge that decides what reaches the
  model: a small search, schema, and call surface. The code is
  `app/deepspace/services/mcp_tool_broker.py`.
- **ACP** - the Agent Client Protocol, which lets a trusted programmatic
  client drive a conversation over JSON-RPC. The bridge is
  `app/deepspace/services/acp_bridge.py`, and the setting that turns it on
  is `deepspace_acp_enabled`.
- **Type graph** - the classes, functions, methods, interfaces, and types
  a workspace declares, and the references between them. The code is
  `app/deepspace/services/type_graph.py`.

### The deployment

- **Deployment** - one running installation of the product, with its own
  settings, its own secrets, and its own data.
- **Settings** - the deployment's configuration, read from the environment
  and from a `.env` file. The class is `Settings` in
  `app/core/config.py`, and every name carries the `AKS_` prefix.
- **Tenant** - one account's slice of the deployment. Every row DeepSpace
  owns carries a tenant id. The code is `app/auth/tenancy.py` and
  `app/auth/models/tenant.py`.
- **User** - one person inside a tenant. The code is
  `app/auth/models/user.py`.
- **Role** - a named set of permissions a user holds. The code is
  `app/auth/roles.py` and `app/auth/rbac.py`.
- **Local mode** - the local, single-machine experience, where the app runs
  on SQLite and a request with no token is signed in as the local operator.
  The check is in `app/auth/dependencies.py`, in `get_auth_context`.
- **Local operator** - the user the local mode signs in: the tenant
  `00000000-0000-0000-0000-000000000001`, the user
  `00000000-0000-0000-0000-000000000002`, with every permission. The
  seeding is in `backend/run_backend.py`, in `seed_local_admin`.
- **Access token** - the short-lived bearer token the API checks. The
  claims are built in `app/auth/dependencies.py`, in
  `create_access_token`.
- **Refresh token** - the longer-lived secret that buys a new access
  token. The routes are `/api/v1/auth/login` and
  `/api/v1/auth/refresh`, in `app/auth/api.py`.
- **API key** - a long-lived credential for a program, named with the
  `dx_` prefix. The model is `app/auth/models/api_key.py`.
- **Provider** - a model service the deployment is connected to. The
  configuration is `app/providers/models/provider_config.py`.
- **Provider secret** - a provider's API key, stored encrypted. The code
  is `app/providers/services/provider_secret_crypto.py`.
- **Keyring** - the set of encryption keys a deployment holds, named by
  key id. The setting is `provider_secret_keyring_json`.
- **Model route** - one model a conversation or a child may run on. The
  code is `app/deepspace/services/model_selection.py`, and the route is
  `GET /api/v1/deepspace/chats/models`.
- **AverQel home** - the directory the local installation writes to,
  `~/.averqel-harness` by default. The code is `app/core/averqel_home.py`.
- **Caddy** - the TLS gateway in front of the Docker stack. The
  configuration is `backend/ops/caddy/Caddyfile`.
- **SearXNG** - the web-search provider used by DeepSpace. Docker deployments
  reach it at `http://searxng:8080`; the standalone local app supervises it at
  `http://127.0.0.1:8888`. The settings are `searxng_base_url` and
  `searxng_local_runtime_enabled`.

## What to watch for

- **Session** names three different things. A **shell session** is a live
  terminal, in `app/deepspace/services/terminals.py`. A **sign-in
  session** is a recorded login, in `app/auth/models/auth_session.py`. The
  **session log** is the typed event record, in
  `app/deepspace/services/session_events.py`.
- **Mode** names three different things: the agent mode, the sandbox mode,
  and the model selection mode. The routes are separate and the settings
  are separate.
- **Plugin** and **extension** are not the same. An extension is
  declarative and carries no code. A plugin may carry code.
- **Queue** names two different things: the prompt queue of a
  conversation, and the Celery queues the worker listens on.
- **Model** names two different things: the model a conversation runs on,
  and the model of a provider in the deployment's own list.
- **Approval** and **permission** are not the same. An approval asks the
  user once. A permission mode decides what may happen without asking.
- The file names here are the ones in this repository in October 2026. A
  rename moves the term with it.
- Some words in this glossary are the product's own. A reader who meets
  "trajectory" in the interface rather than in this manual will not find
  it there.
