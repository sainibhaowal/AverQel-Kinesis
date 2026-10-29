# 39. Goal-Driven Conversations

DeepSpace conversations can carry a named, durable **goal** that survives compaction and turn boundaries. The goal round driver automatically continues the conversation until the goal is reached, paused, or its round limit is hit.

## Use case

- The user states a long-running objective ("migrate the auth module to JWT") and wants the agent to keep working on it across multiple turns
- The agent needs a standing reference point that survives history compaction and context trimming
- The deployment wants to measure progress toward an explicit target rather than an open-ended chat

## Why it matters

- Without a goal, the agent has no persistent "north star" after its context is compacted
- Goal rounds are automatic: the driver queues continuation turns, so the user doesn't need to keep prompting
- The goal's phase (`active`, `paused`, `blocked`, `complete`) and revision give a clear audit trail

## How it works

### Data model

Each conversation can have multiple goals (`deepspace_goals` table):

| Field | Description |
|-------|-------------|
| `name` | Unique within the conversation |
| `content` | The goal statement |
| `phase` | `active`, `paused`, `blocked`, `complete` |
| `revision` | Optimistic lock for concurrent updates |
| `max_goal_rounds` | Cap on automatic continuations (default 3) |
| `rounds_started` | How many continuation rounds have run |

### The driver

`GoalRoundDriver` runs after each turn settles (`_maybe_continue_goals`):

1. Finds active goals for the conversation
2. For each goal, checks `rounds_started < max_goal_rounds`
3. Adds a continuation message to the conversation (role: user, source: `goal`)
4. Enqueues a turn in the turn queue with the continuation prompt
5. Increments `rounds_started` and `revision`

The queued turn is picked up by the dispatcher and runs like any other turn.

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_goal_driver_enabled` | bool | `true` | Master switch |
| `deepspace_goal_max_rounds_default` | int | `3` | Default `max_goal_rounds` for new goals |

### API

```
GET    /api/v1/deepspace/chats/{conversation_id}/goals
```

The HTTP route is read-only and returns the goal projection used by the
composer. Goal creation and mutation currently happen through the native
`goal_set` tool (or the registered goal slash commands), and `goal_read`
returns the model-facing projection. There is no goal-specific HTTP
create/update/delete route in the current API.

### Frontend

The **Goal Bar** above the composer polls the read-only goals route and shows
active goal names, phases, and round progress. It does not currently open an
editor. Pause, resume, block, and complete operations are performed through
`goal_set` or the corresponding slash commands.

## What to watch for

- **Round limit**: After `max_goal_rounds` continuations, the goal is marked `complete` automatically
- **Phase transitions**: Only `active` goals continue; `paused`/`blocked` goals wait for user action
- **Concurrency**: The driver uses the turn queue; a busy queue delays continuation
- **Context**: The continuation message includes the goal content so the agent remembers the objective
- **Compaction survival**: The goal lives in the database, not the context, so it survives aggressive compaction
- **Revision conflicts**: Concurrent goal updates fail with a revision mismatch; retry with fresh data
