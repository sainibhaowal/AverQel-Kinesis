# 01. What DeepSpace is

DeepSpace is the conversation-led workspace inside AverQel. The user writes a message, an agent reads it, and the agent works with a model to produce an answer, sometimes by running tools on the machine. Everything the agent does is written to a typed session log, so the whole run can be read back later.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
- [What to watch for](#what-to-watch-for)

## Use case

DeepSpace is for work that needs more than an answer. The user asks for a change to a repository, a report from several sources, a summary of a folder, or a repeated check on a schedule, and the agent does the work with tools instead of describing how to do it.

The typical user is one of three people:

- A developer who wants the agent to read and change files, run commands, and keep a terminal open.
- A researcher who wants the agent to search the web, read the pages, and report with citations.
- An operator who wants the same check to run again on a timetable without anyone typing.

A single question that needs no tools is a poor fit. DeepSpace spends a model request, a tool call, and a log entry on every step, so a plain chat surface is cheaper for it.

## Why it matters

A chat window that shows only the final text cannot be checked. DeepSpace keeps a record of every step, so a reader can answer three questions that a plain chat cannot:

- What did the agent actually do, and in what order?
- Which file, page, or command produced each claim?
- What would the agent do differently if it ran again?

The log is not decoration. The conversation in the interface is a projection of the log, and the log survives a restart, so a long task can be inspected, resumed, or handed to another operator.

## How it works

### The actors

Five things take part in a turn. Each one has a clear job.

| Actor | What it does | Where it lives |
|---|---|---|
| The user | Writes the message, reads the answer, answers the questions the agent asks. | The browser or the desktop app. |
| The agent | Decides what happens next: which model to call, which tool calls to run, when the turn ends. | `app/deepspace/services/chat_service.py` |
| The model | Answers in text, or asks for a tool call by name and arguments. | A provider the deployment configures. |
| The tools | The named operations the model may call: files, shell, terminal, web, delegation, schedules. | `app/deepspace/services/tools/` |
| The workers | The Celery tasks that run turns, child turns, schedules, and indexing outside the request. | `app/deepspace/workers/` |

The model never touches the machine. It names a tool and its arguments, and the product runs the tool under its own policy. That is the whole safety story in one sentence: the model asks, the product decides.

### The vocabulary

These words are used the same way throughout this manual.

| Term | Meaning |
|---|---|
| Conversation | A thread of turns. It carries one workspace, one model choice, one permission mode, and one agent mode. |
| Turn | One user message plus everything the agent did in answer to it. |
| Round | One request to the model and the reply it produced. A turn has as many rounds as the model needs. |
| Step | The record of one round in the typed log. |
| Run | The durable record of a turn's execution, with its own checkpoint and status. |
| Tool call | One request from the model for one tool. |
| Child | A delegated agent with its own conversation, its own model, and its own tools. |
| Teammate | A named child that a conversation addresses by name. |

### The tool loop

The agent is tool-using. The loop is in two methods of the chat service: `_stream_model_round` and `_handle_tool_calls`.

1. The agent sends the conversation and the tool schemas to the model.
2. The model replies with text, or with one or more tool calls.
3. The agent validates the calls, applies the runtime policy, and runs the surviving calls.
4. The results are appended to the conversation and the model is asked again.
5. This repeats until the model answers with text.

The loop has a hard ceiling of 64 model rounds in one turn (`MAX_AGENT_MODEL_ROUNDS` in `app/deepspace/services/chat_service.py`). Chapter 02 covers the phases in full.

### The typed session log

Every turn writes typed events to `app/deepspace/services/session_events.py`. The event names are stable strings.

| Event | Written when |
|---|---|
| `turn/start` | A turn opens. |
| `turn/end` | A turn closes. |
| `step/start` | A model round begins. |
| `step/end` | A model round ends. |
| `tool/call` | A tool call is made, whatever the outcome. |
| `tool/result` | A tool call ends, with its status and its duration. |
| `assistant/message` | The answer is saved, with its citation count. |
| `subagent/spawn` | A child is created. |
| `subagent/descriptor` | The composition a child is resumed from. |
| `subagent/result` | A child finishes a turn. |
| `team/member-spawned` | A teammate is created. |
| `team/message` | A message goes to a teammate. |
| `team/task` | A task on the shared board changes. |
| `sandbox/mode` | The conversation's permission mode changes. |
| `preset/mode` | The conversation's agent mode changes. |
| `workspace/selected` | The conversation's workspace changes. |
| `model/selection` | The conversation's model choice changes. |
| `hook/invoked` | A hook command ran around a tool call. |
| `hook/result` | A hook command returned a decision. |

Five tools let the agent read the log back: `session_search`, `session_trace`, `session_event_read`, `session_event_search`, and `session_event_trace`. A later turn can therefore ask what it already did instead of guessing.

### The model choice

The model is chosen for the turn. With `deepspace_model_auto_choice_enabled` on (the default), the content of the turn decides: a turn with an image wants a model that reads images, a turn that calls tools wants one that calls tools, and a long conversation wants one whose context window holds it. The same turn always picks the same model, so the choice can be explained.

The composer can also name a model directly. A manual choice is per conversation, may name any model the tenant may use, and is recorded as a `model/selection` event. `GET` and `PUT /api/v1/deepspace/chats/{id}/model` read and record it.

### Local-first deployment

DeepSpace runs on one machine with no other service. The default store is SQLite, the default queue is an in-process Redis stand-in, and the default vector search is a local SQLite extension. A request with no usable access token is signed in as the local operator, with the admin role, every permission, and no password.

The local sign-in follows the database, not the environment. It applies while the store is SQLite. Point `database_url` at PostgreSQL and the bearer-token path takes over, so the deployment needs real logins. A deployment whose environment is `production` must also set its own `database_url`, its own secrets, and its own object-storage keys; the defaults are refused there (`app/core/config.py`).

The local sign-in is in `app/auth/dependencies.py`. A request without a usable token gets the fixed local tenant and user ids, and the token id `local-token`.

### How this differs from a plain chat

| Plain chat | DeepSpace |
|---|---|
| The text of the answer is the whole result. | The log of the run is the result; the text is one projection of it. |
| The model answers once. | The model answers, calls tools, reads the results, and answers again. |
| Nothing runs on the machine. | Tools read and change files, run commands, open terminals, and reach the web. |
| Nothing is recorded. | Every call, result, and state change is a typed event. |
| One model, chosen by the user. | The model is chosen per turn, or named by the user, and the choice is recorded. |
| The work stops when the tab closes. | The turn runs in a worker, and the prompt queue keeps the work that is waiting. |

## What to watch for

- **The agent's work is not the same as the answer.** The text can be short even when many tools ran. Read the `tool/call` and `tool/result` events, not only the final message.
- **A turn is not a session.** The session log is the record; the conversation is a projection of it. A restart does not lose the record, but a turn that was interrupted does not resume by itself unless the runtime saved a checkpoint.
- **The provider is the weak point.** DeepSpace cannot repair a model that returns reasoning with no answer, that prints tool syntax as text, or that stops sending data. Each of those ends the turn with a typed error rather than a wrong answer.
- **The local mode is not a multi-user product.** The local operator has every permission and no password, and the local sign-in applies to every request without a usable token. Point the store at PostgreSQL before sharing a deployment.
- **The model does not see the whole tool list.** The model sees a compact discovery schema and loads the exact schema of a tool when it needs it. A tool the model has not discovered cannot be called.
- **Not everything is proved on a real host.** The session log, the tool loop, and the queue are covered by the test suite. The operating-system cage is proved by `backend/tests/unit/test_deepspace_linux_cage_proof.py` on the host that runs it, and no other host is proved by that test.
