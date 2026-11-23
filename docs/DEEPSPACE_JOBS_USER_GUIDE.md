# DeepSpace Background Jobs User Guide

**Audience:** End users running and checking background work in DeepSpace

---

## Overview

DeepSpace runs a queued conversation turn as a **durable background job**. Starting one returns immediately with a job ID; the work continues in the DeepSpace worker, and you can read its status and output later.

A job ID is the UUID of the queued turn (or of the agent run that executed it). It is not a shell-style name such as `bash-a1b2c3d4`, and a terminal background write has its own separate job id read with `terminal_job`.

---

## Starting a Background Job

Background jobs are created by the application, not by a `job_start` tool:

| Action | What Creates the Job |
|---|---|
| A queued turn submitted to DeepSpace | A durable queued-turn record with a UUID |
| A scheduled prompt | A queued turn created by the schedule dispatcher |
| A delegated subagent queued to Celery | An agent run with a UUID |
| A long terminal write with `background=true` | A terminal write job read with `terminal_job` |

The agent can list and inspect its conversation's jobs with the tools below.

---

## Job Tools

### `job_list` — List Your Jobs

Shows queued and running turns for the current conversation.

**Options:**
- `status` (optional): only report jobs with this status, such as `queued` or `running`
- `limit` (optional): 1–50 jobs; default 20

**Output:** `job_count` plus a `jobs` array. Each job carries `job_id`, `status`, `prompt`, `priority`, `sequence`, `error`, and `created_at` / `started_at` / `completed_at`.

### `job_status` — Read One Job

Returns the same record for the job ID you name. A job that belongs to another conversation is reported as not found.

### `job_output` — Read Job Results

Reads the text a job has produced: the model's narration, the results its tools returned, and the answer it wrote.

**Options:**
- `job_id` (required): the UUID from `job_list` or from the turn that started the work
- `wait_seconds` (optional, 0–120): wait for a running job to finish before returning
- `max_bytes` (optional, 1–1048576; default 65536): cap the returned output

**Example:**
```json
{"name": "job_output", "arguments": {"job_id": "01a1f352-6bdc-714b-ae7e-3c1822702869", "wait_seconds": 60}}
```

**Output includes:**
- `output`: the produced text
- `state`: `queued`, `running`, `done`, or `failed`
- `truncated` / `dropped_bytes`: whether the byte budget cut the text

### `job_kill` — Cancel a Job

Sets a queued or running job to `cancelled`. A job that already ended returns `noop` with its final status.

**Options:**
- `job_id` (required): the UUID of the job

**Example:**
```json
{"name": "job_kill", "arguments": {"job_id": "01a1f352-6bdc-714b-ae7e-3c1822702869"}}
```

---

## Job Lifecycle

```
QUEUED → RUNNING → DONE
                 ↘ FAILED
        ↘ CANCELLED (job_kill)
```

The state a reader sees is derived from the agent run once the turn has started; before that it comes from the queued turn.

---

## Tips

1. **Use `wait_seconds`** when you need the result before continuing.
2. **Use `max_bytes`** when you only need the tail of a long result.
3. **Kill unused jobs** with `job_kill` to free the conversation's queue.
4. **Check `job_list`** before starting more long work.
5. **Terminal writes are different** — read them with `terminal_job`, not `job_output`.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "The job id is not a valid identifier" | Job IDs are UUIDs; copy the full ID from `job_list` |
| "No job with that id belongs to this conversation" | The job belongs to another conversation or does not exist |
| Job stays `queued` | No DeepSpace worker is consuming the queue; check the deployment |
| Output truncated | Raise `max_bytes` or read the job again after it advances |
| Terminal work does not appear in `job_list` | Terminal jobs are separate; use `terminal_job` |

---

## Related Commands

- `/permission` — set the agent's permission mode
- `workflow_run` — start a workflow run
- `bash` / `terminal_*` — run shell commands; terminal background writes use `terminal_job`

---

*See also: [DeepSpace Permission Guide](DEEPSPACE_PERMISSION_USER_GUIDE.md), [DeepSpace Workflows Guide](DEEPSPACE_WORKFLOWS_USER_GUIDE.md), [DeepSpace Terminal Guide](DEEPSPACE_TERMINAL_USER_GUIDE.md)*
