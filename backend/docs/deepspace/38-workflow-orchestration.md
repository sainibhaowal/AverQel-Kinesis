# 38. Workflow Orchestration

DeepSpace can execute JavaScript orchestration scripts and named workspace workflows. Scripts fan out subagents through `workflow.agent()`, `workflow.parallel()`, and `workflow.pipeline()`; named workflows run dependency-ready child-agent waves with bounded concurrency.

## Use case

- The user wants the agent to break a complex task into coordinated subagents (e.g., "research three competitors, then synthesize a comparison")
- A deployment wants deterministic, replayable multi-agent workflows instead of ad-hoc delegation
- The agent needs to run the same orchestration pattern repeatedly with different inputs
- The agent needs each child's answer as structured data instead of prose

## Why it matters

- Subagent fan-out is error-prone when managed manually: depth limits, tool budgets, and context windows vary
- The workflow engine provides a typed JavaScript environment with built-in concurrency control, phase tracking, and result aggregation
- Every workflow run is a typed event in the session log; the trajectory shows the full subagent tree
- A child answer that is read as prose has to be re-parsed by the caller; a schema turns it into a checked object

## How it works

### Architecture

1. **Configuration**: `deepspace_workflow_runs_enabled=true` (default)
2. **Entry point**: `workflow_run` native tool takes either a JavaScript `script` or a named workflow plus optional input
3. **Execution**: The script runs inside the typed workflow engine through the Node.js PTC runtime (`NodePtcRuntime`) inside a bubblewrap cage
4. **Host bindings**: `workflow.agent()` asks the host for one child. The host runs the child to completion and answers with the child's text, or with the validated object when the call declared a schema.
5. **Lifecycle**: Every run gets a durable workflow-job handle. Foreground calls await that handle and always dispose it; background calls return the handle immediately and a worker owns execution. The result, cancellation, failure, phase, log, and agent lifecycle are persisted as ordered events.

Named workflows live at `.averqel/workflows/<name>.json`. Each step has an `id`, a `task`, and optional `depends_on` IDs. Independent steps in one wave run concurrently; a dependent wave waits until all required outputs are durable. Each child gets its own database session for event writes, while shared host-tool execution remains serialized for database safety.

A named run is executed by the engine itself, never by calling the tool again. Calling the tool from inside the engine would create a second job, whose run would create a third, and the run would never settle.

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_workflow_runs_enabled` | bool | `true` | Master switch |
| `deepspace_ptc_node_enabled` | bool | `false` | Enable Node.js PTC runtime |
| `deepspace_ptc_node_executable` | str | `"node"` | Node.js executable path |
| `deepspace_ptc_node_timeout_ms` | int | `120000` | PTC process safety watchdog for a single JavaScript process; this is not an additional workflow-wide deadline |
| `deepspace_ptc_node_max_timeout_ms` | int | `600000` | The longest a workflow's own JavaScript process may run |
| `deepspace_ptc_max_tool_calls` | int | `24` | Max host tool calls per `run_code` program |
| `deepspace_ptc_node_max_pending_calls` | int | `128` | Max concurrent host calls |

A workflow's own agent budget bounds its child count, so a workflow may start
up to `max_total_agents` children (at most 128 per run). The
`deepspace_ptc_max_tool_calls` budget governs `run_code` programs, which do
not fan out on purpose.

### Workflow helpers

The script receives a `workflow` object with these methods:

| Method | Description |
|--------|-------------|
| `agent(task, options?)` | Run one child to completion; resolves to its answer, or to `null` when the child failed |
| `parallel(items, mapper, options?)` | Map items concurrently with concurrency limit; resolves to the mapper results in order |
| `pipeline(items, ...stages)` | Run every item through the stages; resolves to one result per item |
| `phase(title)` | Persist and stream the current phase |
| `log(message)` | Persist and stream a bounded log event |
| `args` | This call's input, verbatim |
| `returnValue(value)` | End the workflow script early with a value |

`agent()` options:

| Option | Description |
|--------|-------------|
| `label` | The short name the run's progress reports |
| `phase` | The phase this child belongs to; the current phase is used when it is absent |
| `mode` | `one-shot` (default) or `continuable` |
| `persona` | The persona that shapes how the child works |
| `model` | The model the child runs under |
| `provider` | The provider route the child runs under |
| `tool_allow` | The only tools the child may use |
| `tool_deny` | The tools the child may not use |
| `schema` | A JSON Schema the child's answer must satisfy |

Without a `schema`, `agent()` resolves to the child's text. With a `schema`,
the child is asked for one JSON object and the hook resolves to that object
once it validates, or to `null` when the child failed or did not match.

#### Structured child output

A schema is object-rooted and uses only these keywords: `type`, `properties`,
`required`, `additionalProperties`, `items`, `enum`, `const`, and `oneOf`,
plus the annotation keywords `description`, `title`, `$comment`, `default`, and
`examples`. A schema outside that subset is refused before any child runs, so
a script fails with a readable message instead of producing an unpredictable
answer.

The child's answer is read as JSON, so a child that fenced its object or
surrounded it with prose still yields the object. An answer that parses but
does not satisfy the schema does not end the search; the next candidate is
tried.

#### Per-item pipelines

`workflow.pipeline(items, ...stages)` runs every item through the stages.
Each stage receives `(previous, item, index)`, where `previous` starts as the
item itself, and the hook resolves to one result per item in the original
order. A stage that throws drops that item to `null` and skips its remaining
stages; the other items are unaffected.

The earlier chain form, `workflow.pipeline(stages, {initial})`, runs one value
through the stages in order and is still accepted.

### Example script

```javascript
workflow.phase("Research");

const results = await workflow.parallel(
  ["competitor-a", "competitor-b", "competitor-c"],
  async (name) => {
    const answer = await workflow.agent(`Analyze ${name}'s pricing and features`, {
      label: `research-${name}`,
      tool_allow: ["web_search", "web_fetch"],
    });
    return { name, answer };
  },
  { concurrency: 3 }
);

workflow.log("All research complete, scoring the findings");

const scores = await workflow.pipeline(
  results,
  async (previous, item) => {
    const score = await workflow.agent(`Score the finding for ${item.name}`, {
      label: `score-${item.name}`,
      schema: {
        type: "object",
        properties: {
          name: { type: "string" },
          score: { type: "integer" },
        },
        required: ["name", "score"],
        additionalProperties: false,
      },
    });
    return score;
  }
);

return { scores };
```

A script that predates the `workflow` helpers may still call the host binding
directly, `await tool("subagent", { description, task, mode })`. That call
answers with the child's result in the same shape `workflow.agent()` returns.

### Named workflow documents

A named workflow is a JSON document the workspace stores:

```json
{
  "max_concurrency": 2,
  "steps": [
    { "id": "read", "task": "Read every source file." },
    { "id": "judge", "task": "Judge the findings.", "depends_on": ["read"] }
  ]
}
```

Each step runs as its own child agent. Independent steps run concurrently; a
dependent step receives the completed steps' results in its task. A step
without a `task`, a duplicate step id, or a dependency on an unknown step is
refused when the document is loaded.

### API

```
GET    /api/v1/deepspace/chats/{conversation_id}/workflow-runs
GET    /api/v1/deepspace/chats/{conversation_id}/workflow-runs/{run_id}
POST   /api/v1/deepspace/chats/{conversation_id}/workflow-runs/{run_id}/cancel
GET    /api/v1/deepspace/chats/{conversation_id}/workspace/workflows
GET    /api/v1/deepspace/chats/{conversation_id}/workspace/workflows/{name}
PUT    /api/v1/deepspace/chats/{conversation_id}/workspace/workflows/{name}
DELETE /api/v1/deepspace/chats/{conversation_id}/workspace/workflows/{name}
```

The model-facing `workflow_run` tool starts runs. Set `run_in_background: true`
to return `{status: "background", job_id, run_id}` immediately. The run routes
are tenant/user/conversation scoped; the detail route replays the durable
event stream. The cancel route only marks the matching durable job for
cancellation, and the worker checks that marker before starting and between
host operations.

The workflow-document routes manage the named workflows in one conversation's
workspace. A name may hold only letters, digits, dashes, and underscores, and
a document may not be larger than 200,000 characters. A write is refused when
the conversation runs in read-only mode, and an invalid document is refused
before it is stored.

### Frontend

The **Workflows** tab in the side panel shows queued/running/completed/failed/
cancelled runs, current phase, bounded logs, stop reason, the agent count, the
run's mode, its final result, and the Stop action. Redis realtime events
stream the run's progress into the panel as it happens; the REST snapshot
remains the authoritative recovery path after reconnects or worker restarts.

The **Workflow Editor** overlay, opened from the header above the transcript,
lists the installed workflow documents, opens one for editing, saves it back
to `.averqel/workflows/` in the conversation's workspace, and deletes one.

## What to watch for

- **Node.js dependency**: Requires Node.js ≥ 20 and `bwrap` for the cage
- **Tool budget**: A workflow may start at most `max_total_agents` children, and at most 128 per run
- **Item budget**: One `parallel()` or `pipeline()` call may carry at most 4,096 items
- **Workflow deadline**: The engine has no separate one-hour or fixed workflow-wide deadline. Caller cancellation, worker cancellation, PTC process safety, host-tool timeouts, output limits, and tool/agent caps remain active production safeguards.
- **Timeout**: A single PTC process still has the configured safety watchdog and is killed when that runtime limit is reached
- **Sandbox**: The PTC runtime runs in a bubblewrap cage; the host must have `bwrap` available
- **Subagent limits**: Each `agent()` call spawns a subagent subject to the conversation's depth and count limits
- **Result size**: Large subagent answers may be spilled; the workflow script receives a reference, not the full text
- **Named workflows and the tool policy**: A named workflow starts its own children directly, as it always has. A scripted workflow's children pass through the same `subagent` policy gate a direct delegation passes, so a policy that requires an approval refuses the child rather than starting it
