# 05. Teammates and the swarm

A teammate is a delegated child that the conversation addresses by name. Teammates share one task board, the agent sends them messages, and a message sent to a teammate that is not working waits for it. A conversation may hold children and teammates at the same time, and all of their work is recorded in the same log.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
- [What to watch for](#what-to-watch-for)

## Use case

A teammate fits work that several agents do together over a period of time, rather than one answer each.

- A codebase review split by area, with one reviewer per area and a board that says which area is done.
- A long investigation where one teammate follows the logs and another follows the configuration, and the agent that owns the team reads both answers.
- A piece of work that must be picked up later, by a teammate that is not running at the moment.

A teammate is the wrong tool for a single question. Use `subagent` for that.

## Why it matters

- **A name is an address.** The agent writes `alice`, and the message goes to the child that answers to `alice` in this conversation. Nothing else is needed to reach it.
- **A message is never lost.** A message for a teammate that is away is written into that teammate's own queue and is answered when the teammate works next.
- **The board is shared and checked.** Every update names the revision it read, so two teammates cannot overwrite each other's work without noticing.
- **Everything is in one place.** The team, its messages, its board, and every child's turn are in the conversation's typed log, under the same tenant, user, and conversation.

## How it works

### A teammate is a named child

`spawn_teammate` creates a teammate with `AgentTeamService` in `app/deepspace/services/agent_team.py`. The service uses the delegation machinery to build the teammate, so a teammate is exactly a continuable child with these differences:

- It has a name, and the name is how the agent addresses it.
- It starts at depth 1 with the status `idle`, because it waits for a message before it works.
- Its label is its name.
- It has its own row in the team table, beside the child row.

The name must be one to 64 characters and must match `^[A-Za-z0-9][A-Za-z0-9_-]*$`. A name may not be used twice in the same conversation.

| Argument | Required | What it does |
|---|---|---|
| `name` | yes | The name the agent addresses the teammate by. |
| `provider` | no | The provider route the teammate runs under. Omit to inherit the parent's. |
| `model` | no | The model the teammate runs under. Omit to inherit the parent's. |
| `persona` | no | A persona that shapes how the teammate works, up to 4 000 characters. |
| `tool_filter` | no | The only tools the teammate may use, up to 64 names. |

### The limits

| Setting | Default | What it decides |
|---|---|---|
| `deepspace_agent_team_enabled` | on | Whether the deployment offers teams at all. When it is off, every team tool answers that the deployment does not offer agent teams. |
| `deepspace_agent_team_max_members` | 8 | The number of teammates one conversation may hold. |

A `spawn_teammate` call that would exceed the member limit is refused, and the refusal names the limit.

### Messaging a teammate

`team_send_message` takes a `to` name and a `body` of up to 20 000 characters, and does two things.

1. It records the message. The message state is `delivered` when the teammate is running, and `queued` when the teammate is not.
2. It puts the message into the queue of the child that works the teammate, and asks the worker to run that child's turn.

Because the message is in the child's own queue, a teammate that is away still answers it when it resumes. The worker claims that queue under a row lock, so a message that arrives at the same moment as the claim is not lost, and one worker task runs up to 8 turns, so a message that arrives while a turn is running is answered by the next turn of the same task.

A teammate may hold at most 20 queued messages. A message sent past that limit is refused and the queue is left as it was.

The call does not return the teammate's answer. The answer is read with `team_get_member`.

The round trip is proved against a real model provider as well as against a
scripted one. The live proof sends one short message to a teammate and waits
for the answer the provider returned, and it skips when the deployment holds
no provider key.

### Listing the team

`team_list_members` is the canonical compact roster read. It returns stable teammate identities, lifecycle state, and a durable team revision. `list_teammates` remains a compatibility alias that also includes provider, model, tool-filter, and answer-preview details. Use `team_get_member` when one teammate's detailed result is needed; do not poll the roster for completion.

A teammate's state is one of three:

| State | Meaning |
|---|---|
| `idle` | The teammate exists and is not working. |
| `running` | The teammate's turn is running now. |
| `finished` | The teammate's last turn ended with a completed answer. |

A teammate whose last turn did not complete goes back to `idle`, so the agent that owns the team can tell the two apart and send the work again.

### The shared board

The board is the conversation's list of tasks. Every teammate of the conversation reads the same board, so a task is how work is handed over.

| Tool | Mode | What it does |
|---|---|---|
| `team_task_create` | write | Creates a task on the board. |
| `team_task_list` | read | Lists the tasks on the board with their status, owner, dependencies, and revision. |
| `team_task_update` | write | Changes a task, naming the revision the caller read. |
| `list_teammates` | read | Lists the team and what each teammate last reported. |
| `team_list_members` | read | Lists the compact canonical roster and its durable revision. |
| `team_get_member` | read | Reads one teammate's detailed status and last answer. |
| `team_send_message` | write | Sends a message to a named teammate and starts its turn. |
| `spawn_teammate` | write | Creates a named teammate. |
| `wait_agent` | read | Waits for a change in the team. |

`team_task_create` takes a `title` of up to 200 characters, an optional `body` of up to 20 000 characters, and an optional `depends_on` list of task ids. A dependency must be a task of the same conversation, and an owner must be a teammate of the same conversation; both are checked when the task is created.

A task starts at revision 1 and in the state `pending`. The states are `pending`, `in_progress`, `blocked`, and `completed`.

### Revisions

`team_task_update` requires the revision the caller read. If that revision is not the current one, the update is refused and the refusal says what the current revision is and asks the caller to read the task again.

This is the whole point of the revision. Two teammates that read the same task at the same revision and both try to update it cannot both succeed: the first update moves the revision on, and the second is refused with the current number. The second teammate has to read the task again and apply its change to what the task says now.

Every successful update adds one to the revision and writes a `team/task` event with the task id, the action, the status, the owner, and the revision.

### Waiting for a change

`wait_agent` waits for the next committed team change. Local waiters are woken by an after-commit notification; a coarse database revision check remains only as a recovery fallback for a change made by another process. It watches three things: a teammate's state, a message, and a task on the board.

| Argument | Default | What it does |
|---|---|---|
| `timeout_seconds` | 30 | How long to wait, at most 300. |

The wait gives up when the timeout passes. An empty change list means nothing changed before the wait ended, and the result says so with `timed_out: true`. When no teammate is running or has queued work that can produce a change, the result is a normal `no_progress: true` response so the caller does not enter a polling loop.

### The events

| Event | Written when |
|---|---|
| `team/member-spawned` | A teammate is created, with its name, provider, model, and state. |
| `team/member-state` | A worker starts, finishes, or safely releases a teammate after a failed turn. |
| `team/message` | A message is delivered to a teammate. |
| `team/task` | A task on the board is created or updated. |

### The swarm picture

A single conversation can hold all of this at once.

- The agent that owns the conversation.
- Up to 16 delegated children (`deepspace_subagent_max_children`).
- Up to 8 named teammates (`deepspace_agent_team_max_members`).
- Children of children, up to 3 levels (`deepspace_subagent_max_depth`).
- One shared board, one message queue per teammate, and one typed log for all of it.

The parent and the children are not separate conversations. They share the tenant, the user, the conversation, the workspace, and the log. A child writes `subagent/spawn`, `subagent/descriptor`, and `subagent/result` into the same event stream as the parent, and a teammate writes `team/member-spawned`, `team/message`, and `team/task`, so the whole run reads as one timeline.

## What to watch for

- **A teammate does not start working when it is created.** `spawn_teammate` makes the record and returns. The teammate works when it is sent a message.
- **A message is not the answer.** `team_send_message` returns the delivery and whether the message was queued. Read `team_get_member` for one answer, or use the compatibility `list_teammates` detail view.
- **A teammate is a child, so it counts against the child budget too.** A teammate holds a child record, and that record is one of the 16 the conversation may hold.
- **The board is per conversation.** A task created in one conversation is not visible in another, even for the same user.
- **A teammate's tool set is wide by default.** `tool_filter` is the only restriction, and it is an allow list, so an empty list means the whole catalogue.
- **A teammate can start children of its own.** A teammate sits at depth 1, so while the deployment's depth limit is above 1 the delegation tools are in its set. Narrow the set when a teammate should only answer messages.
- **A teammate that is running is not interrupted by a new message.** The message waits for the next turn of the same worker task.
- **A refused revision is not an error to retry blindly.** Read the task again, then apply the change to what it says now.
- **The wait is a wait.** `wait_agent` holds the caller's turn open for up to 300 seconds, and that time counts against the turn's budget.
- **Retries are idempotent when keyed.** Write tools accept an optional `idempotency_key`; retrying the same keyed create, message, or task mutation returns the original durable result instead of creating a duplicate.
