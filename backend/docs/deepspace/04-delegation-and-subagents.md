# 04. Delegation and subagents

A child agent is a separate agent that the main agent starts to carry out one self-contained task. The child has its own conversation, its own model, its own tools, and its own history. The parent gets the child's answer back and continues.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
- [What to watch for](#what-to-watch-for)

## Use case

Delegation fits work that can be stated completely in one message and then verified on its own.

- A task that needs a lot of reading and would fill the parent's context with material the parent does not need.
- Several tasks of the same shape that can be done one after another, each with its own answer.
- A task that needs a different model, a different persona, or a smaller set of tools than the parent has.

Delegation does not fit work that needs the parent's context, work that depends on a decision the parent has not made, or work that must finish before the parent continues. A child has no way to ask the user anything.

## Why it matters

- **A child is durable.** The child is a row in the database with its own identity, its own status, and its own queue. It survives a restart, and a later message can continue it.
- **A child has its own budget.** The child runs the same tool catalogue as the parent, narrowed by the agent mode the parent runs in, with its own round budget, its own history length, and its own list of permitted tools.
- **A child cannot take over.** The child's answer is text that the parent reads. The child never writes to the user's conversation, and it never sees the parent's private reasoning.

## How it works

### What a child is

A child is created by `new_child_record` in `app/deepspace/services/subagent_runner.py` and stored as a `DeepSpaceSubagentRun` row. The row carries:

| Field | What it holds |
|---|---|
| `parent_run_id` | The run that started the child. |
| `parent_agent_id` | The immediate parent, when the parent is itself a child. |
| `delegation_depth` | How far below the root agent the child sits. |
| `mode` | `one-shot` or `continuable`. |
| `origin` | `subagent` or `fork`. |
| `label` | The short description the parent gave the child. |
| `agent_provider`, `agent_model` | The route the child runs under. |
| `persona` | The persona that shapes how the child works. |
| `tool_filter` | The allow list and the deny list of tool names. |
| `descriptor` | The versioned composition a later turn rebuilds the child from. |
| `turn_history` | The child's own exchanges, oldest first. |
| `pending_messages` | The child's own queue of messages waiting for its next turn. |
| `status` | `running`, `idle`, or the reason the last turn ended. The record carries the child's work state, not the state of the work it was given. |
| `turn_count`, `result_text`, `stop_reason`, `last_error` | What the child last did. |

### One-shot and continuable

The two modes differ in what happens after the first turn.

- **One-shot** runs once and ends. For the in-process backend, the parent receives a queued child id immediately; it reads the answer later with `get_agent` after the background turn finishes.
- **Continuable** stays available. The parent can send it more messages later, and the child answers them in the context it has already built.

`subagent` starts a continuable child by default, and the `mode` argument chooses. `subagent_fork` always starts a one-shot child, because a fork exists to act on the context the parent already has and then get out of the way.

The in-process backend stores the child and its task before dispatching it. With the local eager Celery configuration, a bounded four-thread background pool runs up to four turns at once and accepts up to 64 outstanding turns. With a Celery worker, dispatch is asynchronous through the queue. A parent call returns the child id and whether dispatch was accepted; it can start other work without waiting for the child model. If dispatch is not accepted, the task remains in the child's durable message queue. Out-of-process provider backends still run inline because their per-task environment is not persisted in the queue payload.

External providers are operator-configured one-shot adapters: ACP, Claude Code,
Codex CLI, the optional DSH SDK, and the KineSis SDK CLI. They receive the child's
task, persona, selected model where supported, workspace directory, and configured environment.
The API startup initializes those configured adapters and records the available
provider names in application state; child workers also initialize the
configuration in their own process before dispatch. This registry setup does
not replace the durable SQL/Celery execution path with the in-memory lifecycle
manager.
DeepSpace records the same durable child row and `subagent/start` / `subagent/end`
activation events, but the external program's internal tool calls do not go
through DeepSpace's native tool broker. As a result, external children cannot
use DeepSpace's per-child tool allow/deny filters or agent presets; those
requests are rejected before the child row is created. Use the in-process
backend when DeepSpace must enforce those controls or when the child must be
continuable.

The Codex adapter uses the noninteractive `codex exec --json` JSONL interface,
passes the selected model and persona, and treats missing/failed terminal events
or a missing final agent message as a failed run. Its defaults are the
`read-only` sandbox and `never` approval prompts because an external child has
no DeepSpace user-approval channel. An operator may set `sandbox_mode` to
`workspace-write` when child edits are intended; `danger-full-access` removes
Codex's sandbox boundary and should only be selected for a separately isolated
runtime. Claude Code runs in print mode with a bounded `--max-turns` count and
passes its configured permission mode and selected model, but its local CLI
policy is separate from DeepSpace's tool authorization. Configure CLI
authentication and any additional sandbox policy in the desktop user's trusted
CLI installation; do not treat the DeepSpace role checks as containment for an
external process. ACP applies an explicit model only when the agent advertises
that exact selectable model; otherwise the run fails with a capability error.
ACP does not standardize provider selection, so configure the provider in the
ACP agent profile.

Example settings for a read-only Codex child:

```json
{
  "deepspace_subagent_default_backend": "in-process",
  "deepspace_subagent_providers": {
    "codex": {
      "command": "codex",
      "args": ["exec"],
      "sandbox_mode": "read-only",
      "ask_for_approval": "never"
    }
  }
}
```

A child that has run many turns is still one child against the conversation's child budget.

### Depth

A child may start a child of its own, up to the deployment's depth limit.

| Setting | Default | What it decides |
|---|---|---|
| `deepspace_subagent_max_depth` | 3 | How many levels of children a root agent may start. With the default of 3, a root's child may start a grandchild, and that grandchild may start one more child. |

The child's depth is its parent's depth plus one. A call that would create a child deeper than the limit is refused before the child is created, and the refusal says which level the deployment allows.

The delegation tools are only offered to a child that is still above the limit. A child at the depth limit does not receive `subagent`, `subagent_fork`, `send_message`, `interrupt_agent`, or `list_agents`, so it cannot keep going on its own.

### The descriptor

The descriptor is the versioned record of the composition a child runs under. `build_child_descriptor` writes it, and its current version is 3.

| Field | What it holds |
|---|---|
| `version` | The descriptor format version, currently 3. |
| `mode` | `one-shot` or `continuable`. |
| `provider` | The route family the child runs under. |
| `label` | The child's short description. |
| `agentProvider`, `agentModel` | The provider and model the child uses. |
| `agentReasoningEffort` | The reasoning effort the child uses. |
| `persona` | The persona that shapes the child's work. |
| `toolFilter` | The allow list and the deny list of tool names. |

The descriptor is written to the typed log as its own event, `subagent/descriptor`, so a reader can see how a child was composed without reading the child's whole history.

### The child's own turn queue

A continuable child has a queue of its own. `send_message` appends to `pending_messages` and then asks the worker to run a turn for the child.

- A child may hold at most 20 queued messages. A message sent past that limit is rejected and the queue is left alone.
- The queue is read under a row lock, so a message that arrives while the child's turn claims the queue is not lost.
- A queued message may include up to 10 image IDs from the same tenant, user, and conversation. The worker rechecks those ownership fields before reading bytes, then applies the normal image MIME, size, and count limits.
- The child's turn history keeps the last 40 exchanges. Older exchanges fall off the end.

### The worker

The child's later turns run in the worker, in `app/deepspace/workers/subagent_tasks.py`, as the Celery task `deepspace.subagent_turn`. The task claims the child's queue, runs up to 8 turns, and records the answer on the child after each one. When the selected provider exposes `stream_generate_events`, the child polls its durable inbox while that stream is open. A new message cancels the open provider stream, discards its partial text and tool-call fragments, and starts a fresh request with the message included. Cancellation is checked during the same wait. Providers without the async event-stream interface keep the between-round behavior and cannot be interrupted mid-request.

The task runs on the `deepspace` queue, beside the chat turn itself, so a teammate's turn is not queued behind document extraction.

The child's tools are authorised with the caller's roles and permissions, so a child can do no more than the user could.

### The tools

| Tool | Mode | What it does |
|---|---|---|
| `subagent` | write | Starts a child with a self-contained task and returns the child's answer. |
| `subagent_fork` | write | Starts a one-shot child that inherits the parent's recent history. |
| `send_message` | write | Queues text and optional conversation-scoped images for a continuable child; event-stream providers are stopped and restarted with the message during an active request. |
| `list_agents` | read | Lists a compact child-agent roster with status, depth, and result availability. |
| `get_agent` | read | Reads one child-agent's detailed status and result by id; can wait up to 90 seconds for a queued/running child. |
| `interrupt_agent` | write | Asks for a running child's current turn to stop. |
| `wait_agent` | read | Waits for a change in the conversation's team. Chapter 05 covers it. |

`subagent` and `subagent_fork` take these arguments:

| Argument | Required | What it does |
|---|---|---|
| `description` | yes | A short label naming what the child is for. |
| `task` | yes | The complete task, up to 20 000 characters. |
| `mode` | no | `one-shot` or `continuable`. `subagent` defaults to `continuable`; `subagent_fork` is always `one-shot`. |
| `provider` | no | The provider route the child runs under. Omit to inherit the parent's. |
| `model` | no | The model the child runs under. Omit to inherit the parent's. |
| `persona` | no | A persona that shapes how the child works, up to 4 000 characters. |
| `tool_allow` | no | The only tools the child may use, up to 64 names. |
| `tool_deny` | no | The tools the child may not use, up to 64 names. |
| `skill_allow` | no | The only skills the child may see, up to 64 names. Omit to inherit the parent's skills. |
| `skill_deny` | no | The skills the child may not see, up to 64 names. |
| `history_messages` | no | `subagent_fork` only. How many recent messages the child inherits, up to 40. It defaults to 10. |

`send_message` takes `agent_id`, `message`, and optional `attachment_file_ids` (up to 10 supported image files already in the same conversation). The tool rejects IDs from another tenant, user, or conversation, and the worker repeats the ownership check before loading the image. `interrupt_agent` takes `agent_id`. `list_agents` takes `include_completed`, which leaves the children whose last turn already completed out of the list. `get_agent` takes `agent_id` and optional `wait_timeout_seconds` (0-90). A positive value waits once for completion and returns `wait_timed_out` if the child is still queued or running when the bound expires. The wait uses fresh tenant/user/conversation-scoped reads and does not reserve a shared tool-execution slot, so the child can continue using tools.

### The child's skill scope

A child agent resolves the same workspace skill registry as its parent, but its
visible catalog is narrowed by the skill scope it inherits. The scope is built
when the child is created:

- The child begins with its parent's effective skill scope (or the unrestricted
  main-agent scope when the parent has none).
- If the parent specifies `skill_allow`, the child's scope grants only those
  names.
- If the parent specifies `skill_deny`, those names are revoked even if an
  ancestor granted them.
- The resulting scope is stored in the child's durable descriptor as a payload
  (`skillScope`), so a cold-resumed child restores the same visibility without
  the original parent process.
- The child's tool context carries its scope, which filters `skill_list`,
  `skill_load`, `skill_resource`, and the per-turn catalog injection.

A child that loads a skill or reads a resource is filtered by its scope at
lookup time, so it cannot reach an ungranted skill even if it guesses the name.
The parent's grants are enforced end-to-end: the child sees the same filtered
catalog in every turn, including the model-facing catalog the backend injects.

### The child's own tool list

`child_tool_names` decides what a child may call. It starts from the whole native catalogue and removes:

- The five delegation names, once the child is at the depth limit.
- The team tools, unless the child is a teammate. A delegated child is not on the team, so it neither addresses the team nor reads the team's board. A teammate keeps them, because the board is how a teammate is handed work.
- `ask_user`, always. A child has no channel to the user, so a question it asked would never reach them.
- `get_tool_result`, always.
- Native tools are supplied directly to the child after the parent's allowed-tool, depth, and teammate filters are applied. The child does not receive `discover_tools`.
- Every tool the conversation's agent mode does not permit. The child inherits the mode its parent runs in, so a mode that withholds the write tools withholds them from the child too.

It then applies the child's own `tool_allow` and `tool_deny`. An empty allow list means the child may use everything that is left; a deny list always wins.

### The child's loop

A child runs a small loop of its own, in `run_child_turn`.

1. The child resolves its model. A child that named its own provider and model uses the matching candidate; otherwise it inherits the conversation's selected model.
2. The child sends its system message, the persona, any inherited history, its own earlier exchanges, and the task.
3. The model answers with text or with tool calls. The child runs the calls it is allowed and appends the results.
4. The loop ends when the model answers with text, when the round budget of 12 is spent, when the parent asks for a stop, or when the provider request fails.

The child's answer is cut to 20 000 characters before it goes back to the parent. The child's own system prompt tells it to report findings rather than greetings, and never to invent a tool result, a file path, a command output, or a citation.

When the task requires a live tool result, an automatically selected child
skips models whose cached capabilities say they cannot call tools. A manually
selected unsupported model is reported as a blocker instead of silently
answering as if it checked. Children receive the parent's scoped MCP broker
only when a connected server is available; a request to search the MCP tool
catalog is satisfied by that broker search, while a request to act in a
connected service still needs an actual MCP call.

The child's round trip is proved against a real model provider as well as
against a scripted one. The live proof sends one short message to a child
and waits for the answer the provider returned, and it skips when the
deployment holds no provider key. The proof in chapter 05 is the same path
with a named child.

### The events

| Event | Written when |
|---|---|
| `subagent/spawn` | The child record is created, with its mode, origin, depth, and label. |
| `subagent/descriptor` | The composition the child may be resumed from. |
| `tool/call` | The child asked for a tool, with a summary of the arguments. The event carries the child's own id. |
| `tool/result` | That tool answered, with the status and the duration. The event carries the child's own id. |
| `subagent/result` | The child's first turn ended, with its stop reason, its round count, and the tools it used. |

A child's own tool calls are written to the same typed log as the parent's, under the same event names, so a reader can rebuild a child's trajectory from the log. Chapter 07 covers the log.

## What to watch for

- **In-process child turns do not hold up the parent.** The spawn call waits only for the task to be saved and dispatched, then returns the child id. The parent can do other work, then call `get_agent` with a bounded wait once to collect the result. Out-of-process providers remain synchronous.
- **A child sees only what it was given.** A child started with `subagent` gets the task text and nothing else. A child started with `subagent_fork` also gets the parent's recent user and assistant messages, up to `history_messages`.
- **A child has no channel to the user.** The `ask_user` tool is not in a child's catalogue, and a child cannot address the team either. A child that runs out of information says so in its answer.
- **The child's tool set is the parent's, narrowed.** It is the whole native catalogue, less the delegation tools at the depth limit, less the team tools unless the child is a teammate, less `ask_user`, less `get_tool_result`, filtered by the agent mode the parent runs in and by the child's own `tool_allow` and `tool_deny`.
- **A child that runs out of rounds is not a failure.** The outcome is `max_rounds`, the answer holds what the child had, and the diagnostic says the round budget was spent.
- **The team tools belong to a teammate.** A delegated child is offered none of them, whatever the depth, and a teammate is offered all of them. Use `tool_allow` and `tool_deny` to say what the child may use.
- **A child's own tool calls are in the typed log.** They are written as `tool/call` and `tool/result`, with the child's id, so the log holds the child's trajectory and not only its spawn and its result.
- **`include_completed` hides a finished child.** A child whose last turn completed is reported with the status `completed`, so `include_completed=False` leaves it out. A child whose turn ended any other way keeps that reason as its status and is still listed. The record's own status keeps the child's work state, so a child that waits for more work still says `idle`.
- **`send_message` does not return the answer.** It returns the delivery and the number of queued messages. The answer is read later with `get_agent`. For providers with async event streams, a new message cancels the active request and restarts it with the new instruction; other providers apply it at a round boundary or on the next turn.
- **An interrupted child keeps its queued messages.** `interrupt_agent` stops the current turn only. The child stays available for follow-ups.
- **A dispatch failure is not a lost message.** The message stays in the child's queue, so the child still runs it on its next turn.
- **The local background pool is process-local.** It is for the desktop/local eager-worker mode; it is bounded and reports its availability, but it is not a distributed durable queue. Production multi-process deployments should use Celery workers. A process crash can interrupt a local in-flight turn, so inspect the child status and retry the queued task after recovery.
- **Child tools share the conversation execution gate.** The Redis gate limits simultaneous reads across the root and child agents and prevents child writes from overlapping other tools in that conversation. The per-round budget still applies to each provider round; the shared cap is per conversation.
- **Durable runs use a database-backed lifecycle coordinator.** SQL/Celery turns are admitted under the conversation row lock, claim their queued messages under the child row lock, and hold a renewable fencing-token lease. A replacement worker can recover an expired lease; the old activation cannot publish settlement after its token is replaced. Interrupts are persisted for the selected child and its descendants. The process-local `SubagentManager` remains limited to resident in-process activations and is never the cross-worker authority.
- **Legacy busy rows fail closed.** A busy row without a lease token is treated as owned by an older activation and is not reclaimed automatically, because the new worker cannot prove that the old process has stopped. Such rows require the old worker to settle or an operator to inspect the deployment before recovery.
