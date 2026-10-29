# 03. Parallel execution

A model round may ask for several tool calls at once. DeepSpace runs the read calls together and the write calls one after another, under a fixed budget, a per-call deadline, and a size limit on what the model is allowed to carry. The same limits govern the number of delegated children a conversation may hold at once.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
- [What to watch for](#what-to-watch-for)

## Use case

Parallel execution matters when one round of the model asks for the same kind of work several times over: reading six files, searching three directories, checking four pages, listing the sessions that are open. Run one after another, the turn takes the sum of the times. Run together, it takes the longest of them.

It also matters for correctness. Two write calls that change the same file must not run at the same time, because the second would read a state the first had already changed.

## Why it matters

- **A round is a set of calls, not a list.** The model answers with several calls in one reply, and the product decides how to run them.
- **Reads are safe to overlap, writes are not.** A read does not change anything, so two reads cannot disagree with each other. A write changes the state the next write reads.
- **The budget is fixed by the deployment.** The operator decides how much the machine may do at once, and the operator also decides how large a result may be before it leaves the model's context.
- **Nothing waits forever.** Every call runs under a deadline, and a call that overruns it fails with a code the model can read.

## How it works

### The tool budget

`deepspace_max_concurrent_tools` is the largest number of tool calls one provider round may run at the same time. It defaults to 8 and may be set from 1 to 32.

The budget is enforced by two objects that the chat service creates for each round of a turn:

| Object | What it is | What it covers |
|---|---|---|
| `state.read_semaphore` | An `asyncio.Semaphore` of `deepspace_max_concurrent_tools` permits. | Every call whose tool mode is `read`. |
| `state.write_lock` | An `asyncio.Lock`. | Every call whose tool mode is not `read`. |

A read call takes a permit. A write call takes the lock, so only one write call runs at a time no matter how many permits the read semaphore has.

### Batching

`_plan_tool_execution_batches` in the chat service groups the round's calls before they run. The grouping is in `app/deepspace/services/execution_batches.py` and is called `plan_execution_batches`.

The rule is simple: contiguous read calls go into one parallel batch, and every other call becomes a single-call batch that is run on its own. A write call is also an ordering barrier, so the reads that follow it wait for it.

A call counts as an independent read when all three of these are true:

- The tool is a native tool in the registry.
- The tool's declared mode is `read`.
- The tool is not a binding to a connected MCP server.

MCP calls therefore run one at a time, even when the underlying server could handle more.

The results are put back in the order the model asked for them, not in the order they finished. The model reads its calls in the order it wrote them.

### Which tools read and which write

Each tool declares its own mode in the module that owns it, and the runtime policy reads the same declaration, so the two cannot drift apart.

| Mode | Examples |
|---|---|
| `read` | `fs_read`, `fs_grep`, `fs_glob`, `workspace_diff`, `read_image`, `web_search`, `url_read`, `list_agents`, `list_teammates`, `job_list`, `job_status`, `job_output`, `schedule_list`, `team_task_list`, `spill_read`, `session_search`, `session_trace`. |
| `write` | `fs_write`, `fs_edit`, `fs_str_replace`, `fs_delete`, `bash`, `pwsh`, `terminal_open`, `terminal_write`, `subagent`, `subagent_fork`, `send_message`, `spawn_teammate`, `team_send_message`, `team_task_create`, `team_task_update`, `schedule_create`, `job_kill`. |
| `interactive` | `ask_user`. It is not a read, so it takes the write lock the way a write does. |

`todo_write` is a write, except for the `read` and `check` operations, which the policy treats as a read so that reading the plan does not hold the lock.

`subagent` and `subagent_fork` are writes, because they create a child. They do not take the write lock: the child's turn runs in the child's own context, so a slow child does not hold the parent's lock while it works. Chapter 04 covers the child case.

### A worked example

A model round asks for five calls: two file reads, one file write, and two more file reads.

| Order | Call | Mode | Batch |
|---|---|---|---|
| 1 | `fs_read` on `a.md` | read | Batch 1, run in parallel. |
| 2 | `fs_read` on `b.md` | read | Batch 1, run in parallel. |
| 3 | `fs_write` on `c.md` | write | Batch 2, run alone. |
| 4 | `fs_read` on `d.md` | read | Batch 3, run in parallel. |
| 5 | `fs_read` on `e.md` | read | Batch 3, run in parallel. |

The two reads in batch 1 run at the same time and take one permit each. The write in batch 2 waits for batch 1 to finish and holds the lock by itself. The two reads in batch 3 wait for the write, then run together.

The reads in batch 3 see the file that batch 2 wrote, because the write finished first. The reads in batch 1 do not, because no write could run between them.

### Two limits on parallel work in a managed task

When a conversation is working through a saved task plan, the rules tighten. The lifecycle of the plan must stay an accurate record, so one provider turn may make one ordered call at a time. The exception is a group of independent native read calls: at least two of them, and no more than `deepspace_max_concurrent_tools` of them, none of them `discover_tools`, none of them an MCP call, and all of them `read`.

### The per-call deadline

Every call runs under a wall-clock budget, so a tool that never returns cannot stall a turn. The budget belongs to the tool pipeline in `app/deepspace/services/tool_pipeline.py`, and it is chosen in this order:

1. The tool's own deadline, when the tool declares one. A tool declares its deadline in the module that owns it, and the chat service hands the pipeline the map of those deadlines.
2. The budget the caller named.
3. `deepspace_tool_timeout_seconds`, the deployment's default, which is 120 seconds.

A call that overruns its budget fails with the code `TOOL_TIMEOUT` and the category `timeout`. The message the model reads is "Tool execution exceeded its time budget."

The chat service names a budget for every call it makes, so the deployment's setting is what a chat turn runs under. The budget is the deployment's `deepspace_tool_timeout_seconds` (120 seconds by default), bounded by the time left before the turn's own deadline, with a floor of five seconds. A tool that declares a deadline of its own keeps it, because the pipeline applies that deadline first.

The deployment's default of 120 seconds is the pipeline's fallback for a caller that names no budget.

`workspace_diff` is the tool that declares a deadline today. It runs one git command, which has a budget of 15 seconds of its own, so the call runs under 20 seconds and a slow repository cannot hold the turn.

### The result spill

A tool result that is too large for the model's context is not carried in full. The result is written to durable, content-addressed storage, and the model receives a compact reference.

| Setting | Default | What it decides |
|---|---|---|
| `deepspace_spill_threshold_chars` | 24 000 | The character count above which a result is spilled instead of carried. |
| `deepspace_spill_max_chars` | 2 000 000 | The largest result the spill store accepts. |

A spilled result reaches the model as a small JSON object: the status `spilled`, the name of the tool that produced it, the `spill_ref` (the content hash), the size in bytes, a preview of the first 4 000 characters, and a message telling the model how to read the rest.

The model reads it back with `spill_read`, which takes the `spill_ref`, an optional `offset`, and an optional `max_characters`. The read returns the window, the total length, whether more remains, and the offset of the next window.

A result larger than `deepspace_spill_max_chars` is one the spill store cannot keep. The call is refused: the model receives a result with the status `refused` and a message that tells it to narrow the call, and the turn continues. The turn is not ended by a result that is too large.

Two separate limits act on a native result. The spill threshold decides whether the result leaves the context at all. A second, smaller limit decides whether the model gets a preview and a `result_ref` instead of the whole text when the round's result budget is spent. That second reference is read with `get_tool_result` and is kept in the cache for fifteen minutes. The spill is the durable copy, kept for as long as the tenant's storage policy allows.

### The child budget

A conversation may hold a bounded number of delegated children at the same time.

| Setting | Default | What it decides |
|---|---|---|
| `deepspace_subagent_max_children` | 16 | The number of children one conversation may hold at once. |
| `deepspace_subagent_max_depth` | 3 | How many levels of delegation a root agent may start. |

A call that would exceed either limit is refused before the child is created, and the refusal is returned to the model as the result of the call. Chapter 04 covers what a child is.

## What to watch for

- **Raising the budget raises the load, not the speed.** Eight read calls that each take twenty seconds take twenty seconds together and one hundred and sixty seconds one after another. The budget only helps when the calls are independent and the machine has the capacity.
- **A write call holds the lock for its whole run.** A long `bash` command and a `terminal_open` are write calls, so each of them delays every other write in the round. A delegated child's first turn is the exception: it runs in the child's own context, so it does not hold the parent's write lock. Chapter 04 covers the child case.
- **The per-call budget is the deployment's, and the shell tool has its own.** `deepspace_tool_timeout_seconds` (120 seconds by default) governs a call from a chat turn, and the shell tool's own `deepspace_shell_timeout_seconds` (120 seconds) and its `timeout_seconds` argument (up to 900) govern the command inside it. A shell command that asks for more than the outer budget is still cut off by the outer budget. A long-running program belongs in a terminal session, which has no such outer cut.
- **A cancelled call stops its process.** The shell tool terminates its command and its process group when the shell's own timeout fires, and it does the same when the outer budget cancels the call. A `TOOL_TIMEOUT` no longer leaves a command running.
- **Reads are only independent when they really are.** Two reads of the same file in one batch see the same content, because no write can run between them. A read that must see the effect of a write has to be in a later round.
- **A tool's own deadline wins over the deployment's.** A tool that declares a deadline runs under that deadline, whatever the deployment's default is. The declaration lives with the tool, so a new deadline is a change to the module that owns the tool.
- **A big result is not lost, but it is not in the context either.** After a spill the model holds a preview and a reference. If the model does not call `spill_read`, the rest of the result stays in storage.
- **A result that exceeds `deepspace_spill_max_chars` refuses that one call.** The model is told to narrow the call, and the turn continues. Raise the ceiling or narrow the tool.
- **The child limits count children, not turns.** A continuable child that has run ten turns is still one child against the budget of 16.
