# 06. Background work

Not every piece of work belongs in the turn that asked for it. DeepSpace has four kinds of background work: the conversation's own jobs, work on a timetable, background terminal writes, and the queue of prompts waiting behind the running one.

## Contents

- [Use case](#use-case)
- [Why it matters](#why-it-matters)
- [How it works](#how-it-works)
- [What to watch for](#what-to-watch-for)

## Use case

Background work is for the cases where the user should not have to wait, and where the work should survive the request that asked for it.

- A long job the agent started: a build, an indexing pass, a test suite.
- A check that must run every hour whether anyone is watching.
- A write to a terminal that takes minutes, such as starting a dev server.
- A second message typed while the first one is still being answered.

## Why it matters

- **The work is durable.** A background job is a row in the database, not a task in memory. It is claimed, run, and recorded, and it can be read back after the fact.
- **One conversation runs one turn at a time.** The queue is what makes that rule safe: a second prompt waits instead of racing the first.
- **A failure is visible and recoverable.** A failed prompt pauses the queue, keeps the waiting prompts, and can be retried as a new attempt.
- **The workers can be separate from the request.** In a broker-backed deployment a turn runs in a Celery worker, so closing the browser does not stop the work. The local development defaults use Celery eager execution and an in-memory broker, so they do not provide a separate durable worker process.

## How it works

### Jobs

A conversation's background jobs are its queued turns. The agent can see them, read them, and stop them.

| Tool | Mode | What it does |
|---|---|---|
| `job_list` | read | Lists the conversation's background work, newest first. |
| `job_status` | read | Reads one job: its status, its prompt, its priority, its error, and its times. |
| `job_output` | read | Reads the text a job wrote and the results its tools returned, and can wait for a running job to finish. |
| `job_kill` | write | Cancels a job that has not finished. |

A job listing takes an optional `status` filter and an optional `limit`, at most 50. `job_output` takes `max_bytes` (at most 1 048 576) and `wait_seconds` (at most 120). While a read waits, it polls every half second.

The states a job reports are `queued`, `running`, `done`, and `failed`. A cancelled job reports `failed`, so the agent cannot tell a cancellation from a crash by the state alone; the error text carries the difference.

`job_kill` on a job that has already ended does nothing and says so.

### Schedules

A schedule is work the product runs later on a timetable. The agent creates and changes them with four tools.

| Tool | Mode | What it does |
|---|---|---|
| `schedule_create` | write | Creates a recurring follow-up for the conversation. |
| `schedule_list` | read | Lists the conversation's schedules. |
| `schedule_update` | write | Changes one schedule. |
| `schedule_delete` | write | Removes one schedule. |

A schedule carries a name of up to 160 characters, a prompt of up to 20 000 characters, and an interval in minutes.

| Rule | Value |
|---|---|
| Shortest interval | 5 minutes |
| Longest interval | 43 200 minutes |
| Default interval | 60 minutes |
| States | `active` and `paused` |

A new schedule's first run is one interval after it is created. A schedule that is paused stays in the list and does not run. The REST routes are the same operations from outside the agent: `POST`, `GET`, `PATCH`, and `DELETE` under `/api/v1/deepspace/schedules`, plus `GET /api/v1/deepspace/schedules/{id}/runs` for the run history.

The Celery beat task `deepspace.dispatch_schedules` runs every minute. It claims the schedules that are due, with a row lock and `skip_locked`, so two beat ticks cannot claim the same schedule. Each claimed schedule gets a run record and a normal DeepSpace run, with the schedule's prompt as the prompt and the `user` role as the authority.

### Terminal jobs

A terminal session keeps a shell alive, so a long write into it should not block the turn. `terminal_write` takes a `background` flag. When it is set, the write runs as a background job and the call returns a `job_id` instead of waiting.

`terminal_job` reads one such job back: its status, the number of bytes written, the reason the write settled with, its error, and its times.

A background terminal write is not the same thing as a conversation job. It is a write into an open terminal session, and the terminal session itself is what keeps running.

### The workers

The workers are Celery tasks. The DeepSpace ones are:

| Task | Queue | What it does |
|---|---|---|
| `deepspace.run` | `deepspace` | Runs one conversation turn. |
| `deepspace.dispatch_turn_queue` | `deepspace_control` | Claims the next waiting prompt and hands it to `deepspace.run`. |
| `deepspace.subagent_turn` | `deepspace` | Runs a delegated child's turn. |
| `deepspace.dispatch_schedules` | `deepspace` | Claims the schedules that are due and starts their runs. |
| `deepspace.index_conversation` | `dataset_indexing` | Builds the derived search rows for a conversation. |
| `deepspace.artifact_create` | `deepspace` | Creates a media artifact. |
| `deepspace.library_upload_finalize` | `library_uploads` | Publishes a finished Library upload. |
| `deepspace.library_file_extract` | `library_extraction` | Extracts the text of a Library file. |
| `deepspace.library_dataset_profile` | `dataset_indexing` | Profiles a dataset. |
| `deepspace.library_media_derivative` | `media_derivatives` | Builds a media derivative. |

The queue dispatch is deliberately kept off the long-running model queue, so that dispatching work cannot starve behind a long turn. The Library work is kept on its own queues for the same reason.

The rest of the product's workers cover the ingestion, the document automation, the maintenance, the notifications, the connectors, and the MCP catalogue. They share the same Celery application.

### The prompt queue

The queue is the list of prompts for a conversation that are waiting to run. A prompt typed while a turn is running is queued, not lost. The composer shows the waiting prompts and offers the actions below.

| Action | Route | What it does |
|---|---|---|
| Enqueue | `POST /api/v1/deepspace/chats/{id}/queue` | Adds a prompt to the queue. |
| List | `GET /api/v1/deepspace/chats/{id}/queue` | Lists the waiting prompts. |
| State | `GET /api/v1/deepspace/chats/{id}/queue/state` | Says whether the queue is paused and why. |
| Pause | `POST /api/v1/deepspace/chats/{id}/queue/pause` | Stops the queue from sending the next prompt. |
| Resume | `POST /api/v1/deepspace/chats/{id}/queue/resume` | Lets the queue run again. |
| Edit | `PATCH /api/v1/deepspace/chats/{id}/queue/{client_request_id}` | Changes a waiting prompt's text in place, keeping its place in the queue. |
| Steer | `POST /api/v1/deepspace/chats/{id}/queue/{client_request_id}/steer` | Brings a waiting prompt forward, cancelling the running turn. |
| Retry | `POST /api/v1/deepspace/chats/{id}/queue/{client_request_id}/retry` | Creates a new attempt for a failed prompt. |
| Cancel | `POST /api/v1/deepspace/chats/queued/{client_request_id}/cancel` | Cancels a prompt. |
| Clear | `POST /api/v1/deepspace/chats/{id}/queue/clear` | Removes the waiting prompts. |

The rules the queue follows:

- **One turn at a time.** A conversation has one active turn. A waiting prompt is not sent while another turn is running, awaiting a question, or awaiting an approval.
- **Order.** Waiting prompts are sent by priority first and by arrival order second.
- **A cap.** A conversation may hold 100 turns in the queue, counting the one that is running. The next one is refused with `DEEPSPACE_QUEUE_FULL`.
- **Editing is safe.** An edit names the text the editor read. If the prompt changed in the meantime, the edit is refused and the caller is asked to refresh.
- **A paused queue stays paused.** Resuming checks what happened to the last prompt the queue sent. A prompt that was interrupted leaves the queue paused, because the user decides what happens to the work that is still waiting.
- **A claim is recovered.** A claim that was left behind by a dead worker is released after ten minutes and the prompt is sent again.

### When a prompt fails

A failed prompt never enters the visible queue. The visible queue holds only the prompts that are `queued`, `awaiting_user`, or `awaiting_approval`. The failure belongs to the conversation, where the user sees it next to the turn that failed.

A failure also pauses the queue. The waiting prompts are kept, and the pause reason says so: "The last prompt failed. The queue is paused and the waiting prompts are kept; the error is in the conversation."

A retry is a new attempt, not a rewind. It creates a new queue item with the failed prompt's text, a higher priority, and a link back to the failed attempt through `resume_from_request_id`. When the queue is paused, which it is after a failure, the retry does not start on its own: the user resumes the queue, and the retry never silently starts the work behind it.

An interrupt works the same way from the other side. A prompt that was sent and never finished is an interrupted prompt, and resuming after an interrupt leaves the queue paused.

## What to watch for

- **A job and a terminal job are different things.** `job_list` and friends describe the conversation's queued turns. `terminal_job` describes a background write into a terminal session.
- **A cancelled job reports `failed`.** Read the error text, not only the state, to tell the two apart.
- **`job_output` can only wait so long.** The wait is at most 120 seconds. A job that runs longer needs more than one read.
- **A background terminal write does not survive a restart.** The terminal job store is in memory. The terminal session it wrote into is a different thing with its own rules.
- **A schedule runs with the `user` role.** The scheduled run is not more privileged than the person who made the schedule, and not less either.
- **A schedule does not run immediately.** The first run is one interval away, so a five-minute schedule waits five minutes.
- **A paused queue sends nothing.** A prompt queued while the queue is paused stays queued until the queue is resumed.
- **A failed prompt is not in the visible queue.** It is in the conversation, with its error. The queue's visible list is not the history of what went wrong.
- **The turn that is running is not in the visible queue either.** The running turn is shown by the conversation and by the run record.
- **A stale claim is released after ten minutes.** A prompt that was sent to a worker that died comes back and runs again. If the first attempt had already changed something, the second attempt sees that change.
