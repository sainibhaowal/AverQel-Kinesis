# 02. The agentic loop

A DeepSpace turn is one user message plus everything the agent did in answer to it. The turn runs in four phases: prepare the request, stream one model round, handle the tool calls that round asked for, and settle the answer. The loop repeats phases two and three until the model answers with text.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
- [What to watch for](#what-to-watch-for)

## Use case

The loop is what makes DeepSpace different from a chat that answers once. The user needs it when the answer depends on something the model cannot know: what is in a folder, what a command printed, what a page says, what a colleague wrote.

It is also what the user needs when the work is long. A turn that takes twenty tool calls and ten minutes must still end cleanly, must not repeat itself, and must leave a record that says what happened.

## Why it matters

Three properties of the loop are worth stating plainly.

- **The model asks, the product acts.** The model names a tool and its arguments. The product validates the call, applies the policy, and runs it. The model never runs anything itself.
- **Every step is written down.** Each round writes `step/start` and `step/end`. Each call writes `tool/call` and `tool/result`. The visible answer is the last part of the record, not the record itself.
- **The loop is bounded.** A turn that repeats itself, a turn that calls too many tools, and a turn that runs too long all end with a typed refusal rather than running forever.

## How it works

The code lives in `app/deepspace/services/chat_service.py`. The entry point is `stream_turn`, which runs the four phases in order.

```
_prepare_turn  ->  _stream_model_round  ->  _handle_tool_calls  ->  _settle_turn
        ^                                                                   |
        +----------------- the model asked for tools ---------------------+
```

### Phase 1: prepare the turn

`_prepare_turn` resolves everything the turn needs before the model is called. It settles:

- The provider route and the model.
- The sandbox mode, which is the conversation's permission mode.
- The agent mode, which is the preset.
- The extensions and the plugins, and the tools they add or refuse.
- The tools the preset allows.
- The workspace the conversation works in.
- The conversation's goals and its plan mode.
- The state of the prompt queue.

It also starts the turn's budgets. The loop guard's counters are reset so that a long conversation is not stopped by the work of an earlier turn, and the wall-clock deadline is set.

The phase ends with the request history prepared. When the conversation is long, `deepspace_compaction_keep_recent_messages` (default 24) keeps the recent messages and replaces the older ones with a bounded digest, and a durable summary of the conversation is added as a system message when one exists. `deepspace_tool_result_prune_threshold` (default 8 192 characters) replaces a large stored tool result with an excerpt plus a reference, so nothing is lost, only deferred.

### Phase 2: stream one model round

`_stream_model_round` makes one request to the provider and streams the reply.

- The reply may be text, tool calls, or both.
- Text arrives as `delta` frames and is also collected into the answer.
- Each tool call is parsed, and a call whose arguments are not valid JSON is reported to the model as invalid rather than run.
- A `step/start` event is written with the round number before the request.

Two things end this phase early:

- **The round ceiling.** A turn may not exceed 64 model rounds (`MAX_AGENT_MODEL_ROUNDS`). The turn stops with a message that says so, and the saved plan and the completed results stay available.
- **The turn deadline.** The turn has a wall-clock budget of 1 800 seconds by default. The budget is the setting `deepspace_agent_max_runtime_seconds`, so a deployment can change it. When the budget is spent, the runtime may extend the deadline up to eight times, saving a checkpoint each time, and then it blocks the turn. The continuation counter is `MAX_DEADLINE_CONTINUATIONS`.

### Phase 3: handle the tool calls

`_handle_tool_calls` validates, polices, batches, and runs the round's calls. The order is fixed.

1. **Validate.** The arguments are checked against the tool's own schema. A call that fails validation is reported to the model and does not run.
2. **Discover.** If the round called `discover_tools`, the other calls in that round are deferred. The schemas the model asked for are available on the next round.
3. **Police.** The runtime policy decides whether the call may run. The policy knows each tool's mode, and the deployment's approval mode decides whether a destructive call pauses for the user.
4. **Batch.** The calls are grouped into parallel read batches and serial write batches. Chapter 03 covers this.
5. **Run.** Each batch runs under the read semaphore or the write lock. Each call runs under a deadline. A call that overruns fails with the code `TOOL_TIMEOUT`.
6. **Record.** `tool/call` is written before the call runs, and `tool/result` is written after it ends, with the status and the duration.
7. **Feed back.** The results are appended to the conversation as tool messages.

The phase ends with a `step/end` event, and the loop returns to phase 2 unless the turn is finished.

### Phase 4: settle the turn

`_settle_turn` writes the answer down and closes the turn.

- The task plan is checked. A plan with unfinished tasks leaves the turn `blocked` and the plan is kept.
- The answer is assembled, the citations are appended, and the assistant message is written.
- The `assistant/message` event is written with the text and the citation count.
- The memory candidates are collected.
- The conversation is queued for background indexing.
- The `turn/end` event is written, and a `done` frame carries the final status.

### The loop guard

The guard watches one conversation's turn and refuses the call that crosses a limit. It is a plain in-memory object with no I/O, so it is cheap to consult on every call. The guard is off only when `deepspace_loop_guard_enabled` is false.

| Setting | Default | What it limits |
|---|---|---|
| `deepspace_loop_guard_max_tool_calls` | 64 | The number of tool calls one turn may make. |
| `deepspace_loop_guard_max_identical_calls` | 4 | The number of times one tool call with the same arguments may repeat. |
| `deepspace_loop_guard_max_repeated_arguments` | 6 | The number of times one set of arguments may repeat, whichever tool carries it. |
| `deepspace_loop_guard_max_turn_seconds` | 900 | The wall-clock budget one turn has, in seconds. |

The limits are checked in a fixed order: the call count, the identical call, the repeated arguments, then the wall-clock budget. Once the guard has stopped a turn it keeps refusing, so a caller that keeps asking gets the same answer.

A refusal carries the code `LOOP_GUARD_TRIPPED` and the name of the limit that was hit: `tool_calls`, `identical_calls`, `repeated_arguments`, or `turn_seconds`. The guard's code is in `app/deepspace/services/loop_guard.py`.

The counters hold the calls of the turn as a whole, so a call that repeats after an interruption still counts.

### The approval pause

Two things can stop a turn and wait for the user.

**The approval mode.** `deepspace_approval_mode` is `never` by default, which runs destructive calls unattended. Set it to `ask` and the tools in `APPROVAL_REQUIRED_TOOLS` pause instead: `write`, `edit`, `delete`, `sandbox_execute`, `fs_write`, `fs_edit`, `fs_delete`, `fs_str_replace`, `bash`, and `pwsh`. An extension may name more tools that always ask.

**A hook that asks.** A hook is an external command that watches a tool call. A hook that answers `ask` does not refuse the call. The call does not run, the turn pauses, and the approval request carries the hook's own question. When the user approves, the call runs and the hooks are not asked about it again.

While a turn is paused the runtime records a checkpoint with the phase `awaiting_approval` and emits `permission_request` and `approval_request` frames. The user answers with `POST /api/v1/deepspace/chats/{conversation_id}/approvals/{approval_id}`.

A third kind of pause is a question. The `ask_user` tool asks the user for one piece of missing information and the turn ends with the status `awaiting_user`. A plan that is ready for review ends the turn with `awaiting_plan_review`.

### How a turn ends

A turn ends with exactly one status. The status is carried in the `done` frame, in the run record, and in the queue record.

| Status | Meaning |
|---|---|
| `ready` | The model answered and the answer was saved. The queue record for this prompt is `completed`. |
| `awaiting_approval` | A call needs the user's decision before it may run. |
| `awaiting_user` | The agent asked the user a question. |
| `awaiting_plan_review` | A plan is with the user for review. |
| `blocked` | The turn stopped with work still open: the plan is not complete, the model printed tool syntax as text, or the deadline was spent. |
| `failed` | The turn ended with an error. The error code names the cause. |
| `cancelled` | The user cancelled the turn. |

A turn that pauses for an approval, a question, or a plan review is not finished. Its queue record stays active so that a later prompt cannot overtake it.

The error codes a failed turn carries include `LLM_EMPTY_RESPONSE`, `LLM_REQUEST_TIMEOUT`, `LLM_PROVIDER_CIRCUIT_OPEN`, `DEEPSPACE_TOOL_FAILED`, `DEEPSPACE_STREAM_FAILED`, and `DEEPSPACE_CONTINUATION_PERSISTENCE_FAILED`.

## What to watch for

- **A blocked turn is not a failed turn.** A blocked turn kept its plan and its completed results, and the next message continues from there. Read the checkpoint before retrying.
- **The `turn/end` event always carries `status: completed`.** The event does not report the real outcome. Use the `done` frame or the run record to learn whether the turn was blocked, cancelled, or failed.
- **A finished turn reports `ready`, not `completed`.** The `done` frame carries `ready` when nothing went wrong. The queue record for that prompt is the one that says `completed`.
- **The approval pause stops the whole turn.** The call that waits for approval does not run while it waits, and the other calls in that round are not run either. When the turn resumes, only the approved call runs, and the model is asked again afterwards.
- **A refused tool call is not a tool failure.** The model is told the call was denied and why, and it may try another approach. A failure carries an error code and a category instead.
- **The model can end the turn without answering.** A reply that carries only reasoning, or only tool syntax printed as text, is refused and the turn is retried once, then blocked. DeepSpace does not present such a reply as an answer.
- **The deadline and the loop guard are different limits.** The guard counts calls; the deadline counts seconds. A turn can pass one and fail the other.
- **Nothing here is proved against every provider.** The loop, the guard, the policy, and the queue are covered by the test suite. The behaviour of a specific provider under a specific model is not.
