# 71. The loop guards

DeepSpace runs three separate protections against a turn that never ends: an
advisory **repeat-tool reminder** that nudges the model out of an identical
tool-call loop, the **loop guard** that refuses the call that crosses a hard
limit, and the **cooperative tool timeout** that stops one tool that runs too
long.

## Use case

- An agent keeps calling the same tool with the same arguments and never reads
  the result it already has
- A deployment wants the model to notice the repetition itself, before the
  runtime refuses the call
- A deployment needs a hard ceiling on one turn's tool calls, repeated
  arguments, wall-clock time, and filesystem searches
- A tool hangs, and the turn must not hang with it

## Why it matters

- A repeated call costs a full model round and a provider bill, and returns the
  same information the agent already has
- A refusal that arrives only after the budget is spent costs the whole budget
- A hard ceiling that never triggers turns a runaway turn into an outage

## How it works

### The advisory repeat-tool reminder

`app/deepspace/services/repeat_tool_reminder.py` tracks one chain per
conversation. A chain counts consecutive calls that carry the same tool name
and canonically identical arguments. The canonical form sorts the argument
keys and renders the values as text, so two calls that carry the same
arguments in a different order are the same call.

| Behavior | Rule |
|---|---|
| Chain reset | A different tracked tool, or different arguments, starts a new chain at one |
| Untracked tools | A call to an excluded or unlisted tool neither increments nor resets a chain, so bookkeeping calls interleaved into a loop do not launder it |
| Threshold hit | The configured count produces one reminder |
| Later thresholds | Every threshold after the first produces the detailed reminder |

The first threshold produces the short reminder:

> You are repeating the exact same tool call with identical arguments. Carefully analyze the previous result before calling again: if the task is not complete, try a different approach or different arguments instead of repeating the call.

Every later threshold produces the detailed reminder, which names the tool,
the consecutive count, and a bounded preview of the arguments. A preview that
exceeds the configured length ends exactly with `… (+<omitted> more chars)`.

The reminder is delivered as one synthetic `user` message after the step's
tool results. It is advisory: it never blocks or delays a call, and a tracker
failure never fails the tool call it was watching.

A new user message resets the chain, so a fresh instruction is never treated
as a loop. An approval resume is the same logical turn and keeps its chain.

The tracker is in-memory only, so a conversation resumed from persistence
starts with a fresh chain.

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_repeat_tool_reminder_enabled` | bool | `true` | Master switch for the advisory reminder |
| `deepspace_repeat_tool_reminder_thresholds` | list[int] | `[3, 5, 8]` | Consecutive-call counts that produce a reminder; each count is at least 2 and no count repeats |
| `deepspace_repeat_tool_reminder_include` | list[str] | `[]` | Tool names to track; an empty list tracks every tool |
| `deepspace_repeat_tool_reminder_exclude` | list[str] | `[]` | Tool names never to track |
| `deepspace_repeat_tool_reminder_arguments_preview_chars` | int | `500` | Argument characters a detailed reminder shows, between 1 and 20000 |

An unusable configuration fails loud: `RepeatToolReminderConfig.from_settings`
raises `RepeatToolReminderError` rather than silently changing the reminder's
behavior.

### The hard loop guard

`app/deepspace/services/loop_guard.py` owns the refusal. It bounds one turn's
total calls, repeated arguments, wall-clock time, and filesystem-search calls.
Repetition limits refuse the offending call only; turn-wide and
filesystem-search limits stop the turn when reached.

The guard and the reminder are complementary. The reminder arrives first and
asks the model to change approach; the guard refuses the call that crosses
the limit. The guard's default ceiling for identical calls is
`deepspace_loop_guard_max_identical_calls`, which is 4, so the reminder's
third-call nudge is the first signal the model sees.

### Subagent repeat and round-budget guards

Delegated child turns and teammates in `app/deepspace/services/subagent_runner.py` carry dedicated autonomous loop protections:

| Guard | Constant / Setting | Behavior |
|---|---|---|
| **Round budget** | `deepspace_subagent_max_rounds` (`64`, `0` -> `200`) | On round `N - 1`, injects a wrap-up notice (`Final round budget notice`); runs the final round without tool schemas so partial progress is synthesized cleanly instead of cut off mid-call. |
| **Repeat warning** | `MAX_IDENTICAL_CHILD_TOOL_REPEATS = 3` | Injects an advisory user prompt telling the child to change strategy or summarize gathered evidence. |
| **Forced summary** | `FORCE_SUMMARY_CHILD_TOOL_REPEATS = 5` | Disables tools for the next round and forces a complete summary of verified findings. |

### The cooperative tool timeout

`app/deepspace/services/tool_pipeline.py` carries the wall-clock budget each
tool declares. A tool that declares a deadline runs under it instead of the
deployment's default. A tool that crosses the deadline returns the typed
failure `TOOL_TIMEOUT` with the error category `timeout`, and the turn
continues with the failure in its context.

## What to watch for

- **Two ceilings, one loop**: the reminder's third call arrives before the guard's fourth refusal. Raising `deepspace_loop_guard_max_identical_calls` above the reminder's thresholds is what lets the later reminders fire
- **In-memory state**: the reminder's chains do not survive a process restart. A conversation that resumes starts fresh
- **Model judgment**: the reminder asks the model to change approach; only the guard refuses a call
- **No cross-conversation state**: each conversation has its own tracker, so one busy conversation cannot trip another's reminder
- **Counting is per tool call**: a batch of parallel calls counts each call, so a parallel batch of the same call can reach a threshold in one step
