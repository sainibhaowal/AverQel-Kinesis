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

- **One-shot** runs once and ends. The answer is returned to the parent and the child is finished.
- **Continuable** stays available. The parent can send it more messages later, and the child answers them in the context it has already built.

`subagent` starts a continuable child by default, and the `mode` argument chooses. `subagent_fork` always starts a one-shot child, because a fork exists to act on the context the parent already has and then get out of the way.

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
- The child's turn history keeps the last 40 exchanges. Older exchanges fall off the end.

### The worker

The child's later turns run in the worker, in `app/deepspace/workers/subagent_tasks.py`, as the Celery task `deepspace.subagent_turn`. The task claims the child's queue, runs up to 8 turns, and records the answer on the child after each one. A message that arrives while a turn runs waits in the queue and is answered by the next turn of the same task.

The task runs on the `deepspace` queue, beside the chat turn itself, so a teammate's turn is not queued behind document extraction.

The child's tools are authorised with the caller's roles and permissions, so a child can do no more than the user could.

### The tools

| Tool | Mode | What it does |
|---|---|---|
| `subagent` | write | Starts a child with a self-contained task and returns the child's answer. |
| `subagent_fork` | write | Starts a one-shot child that inherits the parent's recent history. |
| `send_message` | write | Queues a message for a continuable child and asks the worker to run its turn. |
| `list_agents` | read | Lists a compact child-agent roster with status, depth, and result availability. |
| `get_agent` | read | Reads one child-agent's detailed status and result by id. |
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
| `history_messages` | no | `subagent_fork` only. How many recent messages the child inherits, up to 40. It defaults to 10. |

`send_message` takes `agent_id` and `message`. `interrupt_agent` takes `agent_id`. `list_agents` takes `include_completed`, which leaves the children whose last turn already completed out of the list.

### The child's own tool list

`child_tool_names` decides what a child may call. It starts from the whole native catalogue and removes:

- The five delegation names, once the child is at the depth limit.
- The team tools, unless the child is a teammate. A delegated child is not on the team, so it neither addresses the team nor reads the team's board. A teammate keeps them, because the board is how a teammate is handed work.
- `ask_user`, always. A child has no channel to the user, so a question it asked would never reach them.
- `get_tool_result`, always.
- `discover_tools`, when discovery is not offered.
- Every tool the conversation's agent mode does not permit. The child inherits the mode its parent runs in, so a mode that withholds the write tools withholds them from the child too.

It then applies the child's own `tool_allow` and `tool_deny`. An empty allow list means the child may use everything that is left; a deny list always wins.

### The child's loop

A child runs a small loop of its own, in `run_child_turn`.

1. The child resolves its model. A child that named its own provider and model uses the matching candidate; otherwise it inherits the conversation's selected model.
2. The child sends its system message, the persona, any inherited history, its own earlier exchanges, and the task.
3. The model answers with text or with tool calls. The child runs the calls it is allowed and appends the results.
4. The loop ends when the model answers with text, when the round budget of 12 is spent, when the parent asks for a stop, or when the provider request fails.

The child's answer is cut to 20 000 characters before it goes back to the parent. The child's own system prompt tells it to report findings rather than greetings, and never to invent a tool result, a file path, a command output, or a citation.

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

- **The first turn of a child still delays the parent.** `subagent` and `subagent_fork` run the child's first turn inside the parent's tool call, so the parent waits for the answer. The child's turn does not hold the parent's write lock, so the other writes in the round are not held up by a slow child.
- **A child sees only what it was given.** A child started with `subagent` gets the task text and nothing else. A child started with `subagent_fork` also gets the parent's recent user and assistant messages, up to `history_messages`.
- **A child has no channel to the user.** The `ask_user` tool is not in a child's catalogue, and a child cannot address the team either. A child that runs out of information says so in its answer.
- **The child's tool set is the parent's, narrowed.** It is the whole native catalogue, less the delegation tools at the depth limit, less the team tools unless the child is a teammate, less `ask_user`, less `get_tool_result`, filtered by the agent mode the parent runs in and by the child's own `tool_allow` and `tool_deny`.
- **A child that runs out of rounds is not a failure.** The outcome is `max_rounds`, the answer holds what the child had, and the diagnostic says the round budget was spent.
- **The team tools belong to a teammate.** A delegated child is offered none of them, whatever the depth, and a teammate is offered all of them. Use `tool_allow` and `tool_deny` to say what the child may use.
- **A child's own tool calls are in the typed log.** They are written as `tool/call` and `tool/result`, with the child's id, so the log holds the child's trajectory and not only its spawn and its result.
- **`include_completed` hides a finished child.** A child whose last turn completed is reported with the status `completed`, so `include_completed=False` leaves it out. A child whose turn ended any other way keeps that reason as its status and is still listed. The record's own status keeps the child's work state, so a child that waits for more work still says `idle`.
- **`send_message` does not return the answer.** It returns the delivery and the number of queued messages. The answer is read later with `get_agent`.
- **An interrupted child keeps its queued messages.** `interrupt_agent` stops the current turn only. The child stays available for follow-ups.
- **A dispatch failure is not a lost message.** The message stays in the child's queue, so the child still runs it on its next turn.
