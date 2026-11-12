# 38. Workflow Orchestration

DeepSpace can execute JavaScript orchestration scripts and named workspace workflows. Scripts fan out subagents through `parallel()`; named workflows run dependency-ready child-agent waves with bounded concurrency.

## Use case

- The user wants the agent to break a complex task into coordinated subagents (e.g., "research three competitors, then synthesize a comparison")
- A deployment wants deterministic, replayable multi-agent workflows instead of ad-hoc delegation
- The agent needs to run the same orchestration pattern repeatedly with different inputs

## Why it matters

- Subagent fan-out is error-prone when managed manually: depth limits, tool budgets, and context windows vary
- The workflow engine provides a typed JavaScript environment with built-in concurrency control, phase tracking, and result aggregation
- Every workflow run is a typed event in the session log; the trajectory shows the full subagent tree

## How it works

### Architecture

1. **Configuration**: `deepspace_workflow_runs_enabled=true` (default)
2. **Entry point**: `workflow_run` native tool takes either a JavaScript `script` or a named workflow plus optional input
3. **Execution**: The script runs inside the typed workflow engine through the Node.js PTC runtime (`NodePtcRuntime`) inside a bubblewrap cage
4. **Host bindings**: The script can call `await tool('subagent', {...})` to spawn children; the host answers with the child's result
5. **Lifecycle**: Every run gets a durable workflow-job handle. Foreground calls await that handle and always dispose it; background calls return the handle immediately and a worker owns execution. The result, cancellation, failure, phase, log, and agent lifecycle are persisted as ordered events.

Named workflows live at `.averqel/workflows/<name>.json`. Each step has an `id`, a `task`, and optional `depends_on` IDs. Independent steps in one wave run concurrently; a dependent wave waits until all required outputs are durable. Each child gets its own database session for event writes, while shared host-tool execution remains serialized for database safety.

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_workflow_runs_enabled` | bool | `true` | Master switch |
| `deepspace_ptc_node_enabled` | bool | `false` | Enable Node.js PTC runtime |
| `deepspace_ptc_node_executable` | str | `"node"` | Node.js executable path |
| `deepspace_ptc_node_timeout_ms` | int | `120000` | PTC process safety watchdog for a single JavaScript process; this is not an additional workflow-wide deadline |
| `deepspace_ptc_max_tool_calls` | int | `24` | Max host tool calls per script |
| `deepspace_ptc_node_max_pending_calls` | int | `128` | Max concurrent host calls |

### Workflow helpers

The script receives a `workflow` object with these methods:

| Method | Description |
|--------|-------------|
| `agent(task, options?)` | Spawn one subagent; returns its answer |
| `parallel(items, mapper, options?)` | Map items concurrently with concurrency limit |
| `pipeline(stages, options?)` | Chain stages sequentially, passing output to input |
| `phase(title)` | Persist and stream the current phase |
| `log(message)` | Persist and stream a bounded log event |
| `returnValue(value)` | End the workflow script early with a value |

Named workflow documents may also set `max_concurrency` to bound the number of child model turns in one ready wave.

### Example script

```javascript
const results = await workflow.parallel(
  ["competitor-a", "competitor-b", "competitor-c"],
  async (name) => {
    const result = await tool("subagent", {
      description: `Research ${name}`,
      task: `Analyze ${name}'s pricing, features, and market position`,
      mode: "one-shot",
      tool_allow: ["web_search", "web_fetch", "browser_navigate"],
    });
    return { name, ...result };
  },
  { concurrency: 3 }
);

workflow.log("All research complete, synthesizing");
const synthesis = await tool("subagent", {
  description: "Synthesize comparison",
  task: `Compare these results and rank by value: ${JSON.stringify(results)}`,
  mode: "one-shot",
});
return { comparison: synthesis.answer, details: results };
```

### API

```
GET  /api/v1/deepspace/chats/{conversation_id}/workflow-runs
GET  /api/v1/deepspace/chats/{conversation_id}/workflow-runs/{run_id}
POST /api/v1/deepspace/chats/{conversation_id}/workflow-runs/{run_id}/cancel
```

The model-facing `workflow_run` tool starts runs. Set `run_in_background: true`
to return `{status: "background", job_id, run_id}` immediately. The list and
detail routes are tenant/user/conversation scoped; the detail route replays
the durable event stream. The cancel route only marks the matching durable job
for cancellation, and the worker checks that marker before starting and between
host operations.

### Frontend

The **Workflows** tab in the side panel shows queued/running/completed/failed/
cancelled runs, current phase, bounded logs, stop reason, and the Stop action.
Redis realtime events refresh the view quickly; the REST snapshot remains the
authoritative recovery path after reconnects or worker restarts.

## What to watch for

- **Node.js dependency**: Requires Node.js ≥ 20 and `bwrap` for the cage
- **Tool budget**: The script can call at most `deepspace_ptc_max_tool_calls` host tools
- **Workflow deadline**: The engine has no separate one-hour or fixed workflow-wide deadline. Caller cancellation, worker cancellation, PTC process safety, host-tool timeouts, output limits, and tool/agent caps remain active production safeguards.
- **Timeout**: A single PTC process still has the configured safety watchdog and is killed when that runtime limit is reached
- **Sandbox**: The PTC runtime runs in a bubblewrap cage; the host must have `bwrap` available
- **Subagent limits**: Each `agent()` call spawns a subagent subject to the conversation's depth and count limits
- **Result size**: Large subagent answers may be spilled; the workflow script receives a reference, not the full text
