# 07. The session log and the trajectory

DeepSpace writes a typed event for everything the agent does in a turn. The
events are stored in one table, one row per event, and the sequence number of
each row says where it happened. The timeline of a conversation is a projection
of that log, so the path a turn took can be rebuilt exactly, including after a
restart.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
  - [One record per event](#one-record-per-event)
  - [The event names](#the-event-names)
  - [The events one turn writes](#the-events-one-turn-writes)
  - [A child's own tool calls](#a-childs-own-tool-calls)
  - [The five tools that read the log](#the-five-tools-that-read-the-log)
  - [Reading a trajectory after a restart](#reading-a-trajectory-after-a-restart)
  - [Compaction](#compaction)
- [What to watch for](#what-to-watch-for)

## Use case

Read this page when you want to know what an agent actually did in a
conversation, when you are writing a tool that reads a conversation's own
history, or when you are debugging a turn that went in a direction nobody
expected. The five tools named here are the only way the agent reads its own
past, and they are the only way an operator reads a turn from outside.

## Why it matters

- A turn is not a transcript. It is a sequence of calls, results, and
  decisions, and the sequence is what explains the answer.
- The log is durable. It is written to the database as the turn runs, so a
  process restart does not lose it and the conversation's own settings can be
  read back from it.
- The log is the record a hook's audit trail is written to, so a deployment
  can see which guard scripts ran and what they decided.
- Every read of the log is scoped to one tenant and one user. A conversation
  that belongs to somebody else is reported as not found, so an id never
  reveals that something exists.

## How it works

### One record per event

Each event is a row in `deepspace_session_events`.

| Field | What it holds |
|---|---|
| `seq` | The position of the event in the conversation. The number starts at one and never repeats. |
| `event_type` | The name of the event, such as `tool/call`. |
| `turn` | The turn the event belongs to. |
| `step` | The step the event belongs to. One step is one round with the model. |
| `tool_name` | The tool the event concerns, for a call or a result. |
| `payload` | The event's own data, as JSON. |
| `run_id` | The run that produced the event, when the event belongs to one. |
| `subagent_id` | The delegated child the event belongs to, when it belongs to one. |
| `created_at` | The time the event was recorded. |

The session-event module declares `SESSION_FORMAT_VERSION = 1` for the typed
log record format. The durable event row does not have a `format_version`
column; the export path can add a migrated `format_version` to serialized
events, as described in [Session format migration](34-session-format-migration.md).

The `payload` column is encrypted at rest, so a term that appears only in a payload no longer reaches the search; [The session log at rest](20-security-and-encryption.md#the-session-log-at-rest) describes the key sources, the rotation, and what stays readable.

### The event names

| Event | What it records |
|---|---|
| `turn/start` | A turn began. |
| `turn/end` | A turn finished, with the status it settled with. |
| `step/start` | A request to the model began, with the phase it was in. |
| `step/end` | That request finished. |
| `tool/call` | The agent asked for a tool, with a summary of the arguments. |
| `tool/result` | The tool answered, with the status, the duration, and the stages the call went through. |
| `assistant/message` | The agent's answer, with the number of citations it carries. |
| `subagent/spawn` | A child agent was started, with its mode, its origin, and its depth. |
| `subagent/descriptor` | The durable description a continuable child is resumed from. |
| `subagent/result` | A child agent finished, with its stop reason, its rounds, and the tools it used. |
| `plan/mode` | Plan mode was turned on or off. |
| `plan/submitted` | The agent put a plan in front of the user. |
| `plan/reviewed` | The user approved, changed, or rejected the plan. |
| `sandbox/mode` | The sandbox mode of the conversation changed. |
| `preset/mode` | The agent preset of the conversation changed. |
| `workspace/selected` | The conversation changed the workspace it works in. |
| `model/selection` | The conversation changed the model it runs on. |
| `hook/invoked` | A hook ran, with the hook's id, the event, the tool, and how long it took. |
| `hook/result` | That hook decided, with its decision and its reason. |
| `team/member-spawned` | A named teammate was created. |
| `team/message` | A message went to a named teammate. |
| `team/task` | A task on the shared board was created or changed. |
| `feedback/given` | Session feedback was recorded. |

### The events one turn writes

A turn writes its events in this order.

1. `turn/start` when the turn opens.
2. `step/start` for each request to the model.
3. `tool/call` for each tool the agent asked for, whatever the outcome.
4. `tool/result` for each of those calls, with either `status` `completed` or
   `status` `failed`.
5. `step/end` when the request finishes.
6. `assistant/message` when the agent has an answer.
7. `turn/end` when the turn settles.

The mode changes a turn makes are written as their own events, so the log also
records which model, which preset, which sandbox mode, and which workspace the
turn ran under.

### A child's own tool calls

A delegated child's tool calls are written to the same log, under the same
event names. The events carry the child's own id in `subagent_id`, so a reader
can separate a child's trajectory from its parent's, and the child's spawn,
descriptor, and result events sit between the parent's call and the child's
own calls:

| Event | What it records |
|---|---|
| `subagent/spawn` | The child was created. |
| `subagent/descriptor` | The composition the child may be resumed from. |
| `tool/call` | The child asked for a tool. |
| `tool/result` | That tool answered. |
| `subagent/result` | The child's turn ended. |

A child's turn therefore has a complete trajectory in the log: the calls it
made, the results it got, and how it ended.

### The five tools that read the log

| Tool | What it reads |
|---|---|
| `session_search` | The conversations of this account whose events mention a text. It reports them newest first, each with the event that matched. |
| `session_event_search` | The events of one conversation that mention a text, in the order the conversation recorded them. |
| `session_event_read` | One event in full, with the event before it and the event after it. |
| `session_trace` | The whole timeline of one conversation: its turns, its steps, and its tool calls, in order. |
| `session_event_trace` | The lineage of one event: the turn it belongs to, the step it belongs to, and the events of that step. |

A search splits the query on whitespace and asks for events that mention every
word. The words are matched against the tool name, the event name, and the
rendered payload. A quoted phrase is not a phrase search; it is still several
separate words.

The same five reads are available over HTTP for an operator.

| Route | What it returns |
|---|---|
| `GET /api/v1/deepspace/chats/session-query/sessions` | The conversations that mention a text. |
| `GET /api/v1/deepspace/chats/{id}/session-query/events` | The events of one conversation that mention a text. |
| `GET /api/v1/deepspace/chats/{id}/session-query/events/{seq}` | One event with its neighbours. |
| `GET /api/v1/deepspace/chats/{id}/session-query/trace` | The timeline of one conversation. |
| `GET /api/v1/deepspace/chats/{id}/session-query/trace/{seq}` | The lineage of one event. |

### Reading a trajectory after a restart

The settings a turn depends on are recorded in the log, not held in memory.
The model, the preset, the sandbox mode, the workspace, and the plan-mode
state are each read back from the last matching event at the start of the next
turn. A conversation therefore keeps its choices across a restart, and nobody
can change one out of band without leaving a record.

The model-visible conversation history is a separate thing. The agent keeps a
raw window of the last six stored messages before context fitting and request
compaction, not the log. The log is the record of what happened; the message
history is what the model is shown.

### Compaction

A long conversation does not fit in one request, so the runtime shortens it in
stages before the request goes out.

| Stage | What it does | Setting |
|---|---|---|
| Elide old messages | Keeps the system messages, the opening user request, the last few messages, and every complete tool exchange. The rest is replaced by one marker. | `deepspace_compaction_keep_recent_messages`, default 24 |
| Prune long tool results | Replaces an oversized tool result with its head, its tail, and a marker that says the full result is retrievable. | `deepspace_tool_result_prune_threshold`, default 8192 characters |
| Offload surplus images | Replaces the oldest image blocks with a marker once the request carries more image data than the route accepts. | Ten images by default |
| Fit the window | Keeps the newest history that fits the model's verified context window, and protects the current request and every complete tool pair. | The model's own window |

A conversation that runs past the model's window is also summarised. The
summary is built without another request to the model: it is a bounded digest
of the older messages, with the earlier user requests, the earlier answers,
and the file names those messages referenced. The digest is stored with the
conversation, so the next turn starts with the summary in place of the oldest
messages. The summary is reference material: it is marked as such so the
model does not treat the text inside it as an instruction. A request that
carries more than the last six messages also gets a short in-request digest
of the older ones, capped at 2 400 characters.

## What to watch for

- The log is additive. A deployment that does not write typed events loses the
  projections and nothing else.
- The search is a word match, not a phrase match, and it does not read the
  body of an image or a file that a tool returned. It reads the event payload.
- A search reads at most 2 000 events, reports at most 50 conversations, and
  at most 100 events for one conversation. A trace reads at most 2 000 events.
  A long conversation is therefore not fully searchable in one call.
- A search reports the events that match, not the answer. Reading the whole
  payload of an event means a second call to `session_event_read`.
- The excerpt a search returns is 240 characters, and the summary line is 160.
  A long tool argument is not fully visible in a search result.
- The log records that a call was made and how it ended. It does not record
  every byte of a tool result, and it does not replace the message history.
- A child's calls are in the same log as its parent's, under the same event
  names, with the child's own id. A reader that wants one agent's trajectory
  filters on that id.
- The frame log and the typed log are different records. The frame log holds
  the stream a browser replays; the typed log holds the structured events. A
  browser reconnect replays the frames, not the typed events.
- The tests for the typed log are unit tests. Nothing in this release proves
  the log's contents against a live model run.
