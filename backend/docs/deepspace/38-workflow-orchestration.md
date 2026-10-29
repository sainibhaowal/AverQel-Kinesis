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
3. **Execution**: The script runs inside the Node.js PTC runtime (`NodePtcRuntime`) inside a bubblewrap cage
4. **Host bindings**: The script can call `await tool('subagent', {...})` to spawn children; the host answers with the child's result
5. **Lifecycle**: The workflow script runs to completion or timeout; its return value becomes the tool result

Named workflows live at `.averqel/workflows/<name>.json`. Each step has an `id`, a `task`, and optional `depends_on` IDs. Independent steps in one wave run concurrently; a dependent wave waits until all required outputs are durable. Each child gets its own database session for event writes, while shared host-tool execution remains serialized for database safety.

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_workflow_runs_enabled` | bool | `true` | Master switch |
| `deepspace_ptc_node_enabled` | bool | `false` | Enable Node.js PTC runtime |
| `deepspace_ptc_node_executable` | str | `"node"` | Node.js executable path |
| `deepspace_ptc_node_timeout_ms` | int | `120000` | Script timeout |
| `deepspace_ptc_max_tool_calls` | int | `24` | Max host tool calls per script |
| `deepspace_ptc_node_max_pending_calls` | int | `128` | Max concurrent host calls |

### Workflow helpers

The script receives a `workflow` object with these methods:

| Method | Description |
|--------|-------------|
| `agent(task, options?)` | Spawn one subagent; returns its answer |
| `parallel(items, mapper, options?)` | Map items concurrently with concurrency limit |
| `pipeline(stages, options?)` | Chain stages sequentially, passing output to input |
| `phase(title)` | Emit a phase marker for the trajectory |
| `log(message)` | Emit a log event for the trajectory |
| `returnValue(value)` | End the workflow early with a value |

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
```

The HTTP route is read-only and lists runs recorded in the conversation. A
new run starts through the model-facing `workflow_run` tool; there is no
separate HTTP start route.

### Frontend

The **Workflows** tab in the side panel shows all workflow runs for the conversation with status, timing, and results.

## What to watch for

- **Node.js dependency**: Requires Node.js ≥ 20 and `bwrap` for the cage
- **Tool budget**: The script can call at most `deepspace_ptc_max_tool_calls` host tools
- **Timeout**: Scripts exceeding `deepspace_ptc_node_timeout_ms` are killed
- **Sandbox**: The PTC runtime runs in a bubblewrap cage; the host must have `bwrap` available
- **Subagent limits**: Each `agent()` call spawns a subagent subject to the conversation's depth and count limits
- **Result size**: Large subagent answers may be spilled; the workflow script receives a reference, not the full text
