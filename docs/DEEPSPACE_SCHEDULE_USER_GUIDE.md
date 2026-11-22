# DeepSpace Schedules — User Guide

**Date:** 2026-11-19

---

## What It Is

A **schedule** is a recurring prompt that DeepSpace sends back into one conversation on a fixed interval. The agent runs the prompt, reports the result, and the cycle repeats.

**Use it when:**
- You need periodic checks (e.g., "Check disk usage every hour")
- You want automated reports ("Summarize today's commits every morning")
- You want recurring reminders ("Remind me to review PRs every 30 min")

**Skip it when:**
- One-off tasks (use normal chat)
- You need precise cron timing or a calendar-based rule; schedules are simple fixed intervals

---

## What You See in the UI

Schedules are managed from chat. There is no separate schedule management panel in the Kinesis desktop build; ask the agent to list, pause, resume, change, or delete a schedule.

| Location | What It Shows |
|---|---|
| **Chat** | The agent creates and manages schedules with the tools below |
| **Conversation turns** | Each due run appears as a normal agent turn in that conversation |
| **API** | `GET /api/v1/deepspace/schedules` and the runs endpoint |

---

## How to Use It (Natural Language)

| You Say | Agent Does |
|---|---|
| *"Remind me to check disk usage every hour"* | `schedule_create(name="Disk check", prompt="Check disk usage", interval_minutes=60)` |
| *"List my schedules"* | `schedule_list()` |
| *"Pause the disk check"* | `schedule_update(schedule_id="...", status="paused")` |
| *"Change the deploy check to every 30 min"* | `schedule_update(schedule_id="...", interval_minutes=30)` |
| *"Delete the old backup check"* | `schedule_delete(schedule_id="...")` |

The tool arguments are only `name`, `prompt`, and `interval_minutes` for create; `schedule_update` also accepts `status` (`active` or `paused`). The calling conversation is supplied by the runtime, so a schedule created from chat belongs to that conversation.

---

## What You See in Results

### Create

```json
{
  "status": "ok",
  "schedule": {
    "id": "8f14e45f-ea4b-4b1f-a0f2-0b0b1f8e3a11",
    "name": "Disk check",
    "interval_minutes": 60,
    "status": "active",
    "next_run_at": "2026-11-19T15:00:00+00:00"
  }
}
```

### List

```json
{
  "status": "ok",
  "schedules": [
    {
      "id": "...",
      "name": "Disk check",
      "interval_minutes": 60,
      "status": "active",
      "next_run_at": "..."
    }
  ]
}
```

The tool result carries those five fields per schedule. The REST API returns the richer record, including `prompt`, `conversation_id`, `last_run_at`, `last_run_id`, and `created_at`.

---

## Limits

| Limit | Value |
|---|---|
| Minimum interval | 5 minutes |
| Maximum interval | 43,200 minutes (30 days) |
| Default interval | 60 minutes |
| Name length | 1–160 characters |
| Prompt length | 1–20,000 characters |
| Run history returned | Newest 100 runs |

---

## How It Works

1. **Create** — saved with `next_run_at = now + interval`.
2. **Dispatch** — a poller finds due schedules (`status = active` and `next_run_at <= now`), claims them with `SELECT ... FOR UPDATE SKIP LOCKED`, writes a run row, advances `next_run_at`, commits the claim, and enqueues a normal DeepSpace run with the schedule's prompt.
3. **Agent runs** the prompt in the same conversation.
4. **Run recorded** in `deepspace_schedule_runs` with status, error, and timing.

### Who dispatches

| Runtime | Dispatcher |
|---|---|
| Packaged Kinesis desktop | An in-process poller starts automatically when the desktop runtime flag is set and runs once a minute |
| Server with Celery beat | The `deepspace-dispatch-schedules` beat entry runs `deepspace.dispatch_schedules` once a minute |
| Server without Celery beat | An operator can run the DeepSpace proactive daemon (`deepspace_proactive_daemon_enabled=true`), which runs the timed work as a separate process |

The poller only fills in for a missing beat process; all three paths call the same durable dispatcher and keep the same claim rules. A deployment should use one dispatcher path, not several.

### Delivery guarantees

| Property | Guarantee |
|---|---|
| **No double claim** | `SELECT ... FOR UPDATE SKIP LOCKED` plus the run row |
| **At-least-once** | The claim commits before enqueue; a crash after enqueue may repeat a run |
| **No automatic retry** | A failed run stays `failed`; the next interval schedules a fresh run |
| **Pause/Resume** | `status` controls whether the schedule is claimed |

---

## API Endpoints (for Developers)

All routes are under `/api/v1/deepspace/schedules`.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/deepspace/schedules` | Create a schedule (`name`, `prompt`, `conversation_id`, optional `interval_minutes`) |
| `GET` | `/api/v1/deepspace/schedules` | List the caller's schedules |
| `PATCH` | `/api/v1/deepspace/schedules/{schedule_id}` | Update name, prompt, interval, or status |
| `DELETE` | `/api/v1/deepspace/schedules/{schedule_id}` | Delete a schedule |
| `GET` | `/api/v1/deepspace/schedules/{schedule_id}/runs` | List the newest 100 runs |

Create validates that the conversation is a DeepSpace conversation owned by the caller. Every route is tenant- and user-scoped.

---

## Agent Tools

| Tool | Description |
|---|---|
| `schedule_create` | Create a recurring prompt for this conversation |
| `schedule_list` | List this conversation's schedules |
| `schedule_update` | Update name, prompt, interval, or status |
| `schedule_delete` | Remove a schedule |

A schedule of another conversation is reported as `Schedule not found.`; the tools never cross conversation scope.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Schedule not running | No dispatcher is active for this runtime | Desktop: confirm the app runs with the desktop runtime flag. Server: run Celery beat or the proactive daemon |
| Runs happen twice | Two dispatchers or more than one beat scheduler | Use a single dispatcher path; `SKIP LOCKED` prevents duplicate claims within one dispatcher |
| Schedule stays paused | Status is `paused` | `schedule_update(schedule_id="...", status="active")` |
| Runs failing silently | The run's error is stored per run | Read `GET /api/v1/deepspace/schedules/{schedule_id}/runs` and check `error` |
| A due run did not start | The worker queue has no consumer | Check the DeepSpace worker and its queue |

---

## Related Documentation

| Topic | Document |
|---|---|
| Schedule API | `backend/app/deepspace/api/schedules.py` |
| Schedule dispatcher | `backend/app/deepspace/workers/schedules.py` |
| Desktop poller | `backend/app/deepspace/workers/local_schedule_dispatch.py` |
| Proactive daemon | `backend/app/deepspace/workers/daemon.py` |
| Schedule model | `backend/app/deepspace/models/schedule.py` |
| Schedule tools | `backend/app/deepspace/services/tools/schedule.py` |

---

## TL;DR

- **Schedules are recurring prompts** that DeepSpace runs in one conversation.
- **Managed from chat** with `schedule_create/list/update/delete`.
- **Dispatch runs once a minute** — automatically in the desktop app, through Celery beat or the proactive daemon on a server.
- **Runs are recorded** with status and error; the newest 100 are listed by the API.
- **Minimum interval is 5 minutes**, maximum 30 days.
