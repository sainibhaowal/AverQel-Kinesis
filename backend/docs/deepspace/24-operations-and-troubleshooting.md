# 24. Operations and troubleshooting

This chapter is about running the product: the local stack on one machine,
the Docker stack on a server, the processes each one starts, the log files
they write, the health check that says whether the API is ready, the
failures that come up most often, and what the code does and does not limit
on the machine it runs on.

## Use case

- An operator has to start the product and know that it is up.
- A developer has to find the reason a request failed from the logs.
- Someone has to know which port each service answers on before they
  change one.
- A team has to know how much disk and memory the local installation uses
  before they put it on a laptop.

## Why it matters

- The local stack and the Docker stack use different ports. A proxy
  configuration that works on one does not work on the other.
- The logs are structured JSON, not text. Reading them with `grep` for a
  message finds nothing, and reading them with a JSON reader finds the
  answer.
- The health check answers on the database and the cache, not on the model
  providers. A green check does not mean a model answer will come.
- The local installation writes to the user's home directory. Knowing where
  is the difference between "the app is slow" and "the disk is full".

## How it works

### The local stack

One script starts everything:

```sh
./start-local.sh [all|desktop|backend|frontend|test]
```

| Mode | What it does |
|---|---|
| `all` | Starts the API and the web app, and stops both on Ctrl+C. |
| `desktop` | Starts the API, the web app, and the Electron shell. |
| `backend` | Starts only the API. |
| `frontend` | Starts only the web app. |
| `test` | Runs the offline test suites and exits. |

The default mode, `all`, prints what it starts:

- The database is SQLite, at `~/.averqel-harness/db/knowledge.db`.
- The storage is the local filesystem, at `~/.averqel-harness/storage`.
- The logs are at `~/.averqel-harness/logs/backend.log`.
- The cache is at `~/.averqel-harness/cache`.
- The cache and the event bus are an embedded in-memory Redis.
- The tasks run in an in-process eager Celery.
- No Docker container or user-managed background daemon is needed. The API
  supervises the optional local SearXNG process when web search is enabled;
  its first-run installation is described in [chapter 41](41-local-searxng.md).

The script frees ports 10000 and 10030 before it starts, waits for the API
to answer, and then starts the web app. It prints the three addresses it
is serving: the web app on `http://localhost:10030`, the API on
`http://127.0.0.1:10000/api/v1`, and the API's own documentation on
`http://127.0.0.1:10000/docs`.

The API itself is started with `python run_backend.py` from `backend/`. It
reads `PORT` or `BACKEND_PORT` and defaults to 10000. It binds
`127.0.0.1`, and it reloads on a source change unless `RELOAD=false`.

`run_backend.py` prepares the local installation before it serves:

- It writes `AKS_DATABASE_URL` and `AKS_LOCAL_STORAGE_PATH` into the
  environment, pointing at the AverQel home directory, unless the operator
  already set them.
- It starts the embedded in-memory Redis.
- It creates the SQLite tables and seeds the local tenant, the local roles,
  and the local user.
- It seeds the MCP catalogue and prints how many connectors it found.
- It creates the storage directory.
- It starts native SearXNG on `127.0.0.1:8888` when the local runtime is
  enabled, installing it under `~/.averqel-harness/searxng` on first use.

The web app is started with `pnpm start` when a `.next` build exists, and
with `pnpm dev` when it does not. Both use port 10030. The API address the
browser uses comes from `NEXT_PUBLIC_API_URL`, which the local `.env.local`
sets to `http://localhost:10000/api/v1`.

### The Docker stack

The production stack is driven by `backend/scripts/deploy_prod.sh`. It
validates the environment file, refuses to continue when the file is
missing or too open, pulls the images, and brings the stack up with
`docker compose -f docker-compose.prod.yml --env-file <file> up -d`.

The stack is made of these services. The compose file is
`backend/docker-compose.prod.yml`, and every row below names what the
service does and the port it answers on inside the stack.

| Service | What it does | Port inside the stack |
|---|---|---|
| `api` | The FastAPI application. `scripts/start_api.sh` runs the migrations, seeds the integration and MCP catalogues, and starts uvicorn. | 1000 |
| `frontend` | The Next.js application. | 1030 |
| `caddy` | The TLS gateway. It serves the web app, proxies `/api/*` to the API, and proxies the streaming routes with flush disabled. | 80 and 443 |
| `livekit` | The voice transport. It runs the checked-in `livekit-server` binary with the configuration in `backend/ops/livekit/livekit.yaml`. | 7880, 7881 and 7882 |
| `postgres` | The database. | 5432 |
| `redis` | The broker and the cache. | 6379 |
| `minio` | The object storage for documents and uploads. | 9000 |
| `searxng` | The web search provider. | 8080 |
| `clamav` | The malware scanner. A production deployment refuses to start without it. | 3310 |
| `otel-collector` | The OpenTelemetry collector. | 4317 and 4318 |
| `jaeger` | The trace backend the shipped collector configuration exports to. | 4317 |
| `inference` | The local model runtime. `scripts/start_inference.sh` starts it. | 1011 |
| `worker` | The Celery worker. `scripts/start_worker.sh` starts it, and the queue-specific workers beside it take the other queues. | none |
| `scheduler` | The Celery beat scheduler. `scripts/start_scheduler.sh` starts it. | none |

`scripts/start_api.sh` refuses to start in production while `AKS_JWT_SECRET`
or `AKS_REFRESH_TOKEN_HASH_SECRET` still holds its `change-me` placeholder.
It runs `alembic upgrade heads`, because the checkout carries more than one
migration head, and it starts two uvicorn workers by default. The worker
count comes from `AKS_API_WORKERS` and may not be more than 16. The
concurrency limit comes from `AKS_API_LIMIT_CONCURRENCY` and defaults to
200.

`scripts/start_worker.sh` starts the Celery worker. It requires
`AKS_REDIS_URL` and `AKS_DATABASE_URL`, and it listens on the queues
`ingestion_heavy`, `ingestion_light`, `maintenance`, and `deepspace`. The
worker count comes from `AKS_WORKER_CONCURRENCY` and defaults to 4.

`scripts/start_scheduler.sh` starts the Celery beat scheduler. It requires
`AKS_REDIS_URL`. The beat schedule file comes from
`AKS_CELERY_BEAT_SCHEDULE_FILE`, and it falls back to
`/tmp/averqel-celerybeat` when that path is not writable.

The same script starts the proactive daemon instead when
`AKS_DEEPSPACE_PROACTIVE_DAEMON_ENABLED` is `true`. The daemon runs the
same timed work in its own process: it holds an exclusive lock so that only
one daemon runs, it looks for the recurring schedules that are due, and it
stops on `SIGTERM`. The setting is off by default.

### The ports

| Port | What answers there | Where it is used |
|---|---|---|
| 10000 | The API, under `/api/v1`. | The local stack. |
| 10030 | The web app. | The local stack. |
| 1000 | The API, under `/api/v1`. | The Docker stack, behind Caddy. |
| 1030 | The web app. | The Docker stack, behind Caddy. |
| 80 and 443 | The gateway. | The Docker stack, on the public address. |
| 7880 | The voice transport. | The Docker stack. |
| 7881 and 7882 | The voice transport's real-time ports, one TCP and one UDP. | The Docker stack. |
| 5432 | PostgreSQL. | The Docker stack. |
| 6379 | Redis. | The Docker stack. |
| 9000 | MinIO, the object storage. | The Docker stack. |
| 9001 | The MinIO console. | The Docker stack. |
| 8080 | SearXNG, the web search provider. | The Docker stack. |
| 8888 | The supervised local SearXNG process. | The Dockerless local stack. |
| 3310 | ClamAV, the malware scanner. | The Docker stack. |
| 4317 and 4318 | The OpenTelemetry collector. | The Docker stack. |
| 1011 | The local model runtime. | The Docker stack. |
| 11434 | Ollama, when the deployment runs a local model. | A local model provider. |
| 8000 | vLLM, when the deployment runs a local model. | A local model provider. |
| 1234 | LM Studio, when the deployment runs a local model. | A local model provider. |

The CORS list in the settings names the browser origins the API accepts:
`http://localhost:10030`, `http://127.0.0.1:10030`, `http://localhost:1030`,
`http://127.0.0.1:1030`, `http://localhost:3000`, `http://127.0.0.1:3000`,
`http://localhost:5173`, and `http://127.0.0.1:5173`.

The gateway refuses `/api/v1/metrics` with a 403, so the metrics endpoint
is not public.

### The processes

| Process | Started by | What it owns |
|---|---|---|
| The API | `run_backend.py` locally, `scripts/start_api.sh` in Docker. | The HTTP routes, the streaming responses, the approval decisions, and the model requests that run in the request. |
| The Celery worker | `scripts/start_worker.sh`. | The subagent turns, the conversation indexing, the Library work, the schedules, and the DeepSpace control loop. |
| The Celery beat scheduler | `scripts/start_scheduler.sh`. | The timed jobs: the heartbeat, the retention cleanup, the notification outbox, the storage cleanup, and the schedule dispatch. |
| The proactive daemon | `scripts/start_scheduler.sh`, when `AKS_DEEPSPACE_PROACTIVE_DAEMON_ENABLED` is `true`. | The same timed jobs, for a deployment that does not run the beat process. |
| The web app | `pnpm dev` or `pnpm start`. | The interface, and the browser's calls to the API. |
| The Electron shell | `pnpm --dir applications/desktop dev`. | The desktop window. |
| Local SearXNG | `run_backend.py`, when enabled. | The loopback-only web-search endpoint at `127.0.0.1:8888`. |

The tasks are routed to queues by name. `deepspace.run` and
`deepspace.dispatch_schedules` go to the `deepspace` queue.
`deepspace.dispatch_turn_queue` goes to `deepspace_control`, so the short
database coordination it does cannot be starved by a long model turn. The
Library work is split across `library_uploads`, `library_extraction`,
`dataset_indexing`, and `media_derivatives`, so slow text recognition
cannot block a chat turn.

In the local stack there is no separate worker. `redis_url` defaults to
`memory://`, which makes Celery run its tasks eagerly inside the API
process, and `celery_task_always_eager` defaults to true.

### The log files

The application writes structured JSON to two places: the standard output
and a rotating file.

The file is `backend.log` in the `logs/` folder of the AverQel home
directory, which is `~/.averqel-harness/logs/backend.log` by default. It rotates at
10 MiB and keeps 5 backups.

Each line carries these fields: `timestamp`, `level`, `module`, `message`,
`trace_id`, `tenant_id`, `user_id`, `file`, and `line`. The `trace_id`
follows one request, so a request's lines can be gathered with one value.

A filter redacts a value that looks like a secret before the line is
written. A redacted message loses its arguments rather than keeping a
format string with nothing to fill.

The worker and the scheduler write to their own standard output, which the
container runtime collects.

### The health check

Two routes answer under the API prefix.

| Route | What it checks |
|---|---|
| `GET /api/v1/health/live` | The process is running. It answers with the version, the git commit, and the build timestamp. |
| `GET /api/v1/health/ready` | The database and the cache. It answers `{"status":"ok", ...}` when both are reachable. |

The readiness check fails with a 503 and the code `DATABASE_NOT_READY` when
the database does not answer a simple query, and with `REDIS_NOT_READY`
when the cache does not answer a ping. When the deployment turns the
malware scanner on and requires it, a scanner that is not available fails
the check with `MALWARE_SCANNER_NOT_READY`.

A local check is:

```sh
curl -s http://127.0.0.1:10000/api/v1/health/ready
```

### The data directories

Everything the local installation writes lives under the AverQel home
directory, which is `~/.averqel-harness` or the directory named by the
`AVERQEL_HOME` environment variable.

| Directory | What it holds |
|---|---|
| `db/` | The SQLite databases: `knowledge.db`, which holds the conversations and the library, and `vector.db`, which holds the local vector index. |
| `storage/` | The uploaded file blobs. |
| `cache/` | The transient artefacts: the embeddings cache, the search index, and the temporary chunks. |
| `secrets/` | The deployment-level secret files, such as the Composio key. |
| `mcp/` | The MCP server metadata and the configuration cache. |
| `logs/` | The application log files. |
| `exports/` | The export archives the user asked for. |
| `models/` | The local embedding, reranking, speech-to-text, and text-to-speech models. |

The workspaces live under `<local_storage_path>/workspaces`, one directory
per tenant and user.

### The command line

The DeepSpace command line reads the same API the interface uses, so it answers the questions an operator asks while a deployment is running. It needs `AKS_API_URL` and `AKS_API_TOKEN` in the environment, and `--json` prints the raw response instead of the text.

| Command | What it prints |
|---|---|
| `python -m app.deepspace.cli chats` | The conversations of the tenant. |
| `python -m app.deepspace.cli chat <id>` | One conversation. |
| `python -m app.deepspace.cli run <id> "<prompt>"` | Sends a message and prints the answer. |
| `python -m app.deepspace.cli sandbox` | The cage the local shell tools will use. |
| `python -m app.deepspace.cli plugins` | The plugin catalogue. |
| `python -m app.deepspace.cli presets` | The agent modes and what each one allows. |
| `python -m app.deepspace.cli workspaces` | The workspaces the account owns, with their paths. |
| `python -m app.deepspace.cli terminals <id>` | The live terminal sessions of a conversation. |
| `python -m app.deepspace.cli plan <id>` | The plan of a conversation. |

### The common failures

| What you see | What it means | What to do |
|---|---|---|
| `SANDBOX_UNAVAILABLE` | The deployment demanded a cage and the host offers none. The result names the runners the platform supports. | Install bubblewrap or Landlock on the host, set `deepspace_sandbox_backend` to the runner the host has, or set `deepspace_sandbox_required=false` to accept an uncontained command. |
| `TOOL_TIMEOUT` | A tool call ran past `deepspace_tool_timeout_seconds` or past its own deadline. | Raise the budget, or check whether the tool is waiting on something that never answers. |
| `FS_STALE_VERSION` | The file changed after the model read it, so the edit was refused rather than applied to the wrong text. | Read the file again and retry. |
| `FS_AMBIGUOUS_EDIT` | The text to replace appears more than once in the file. | The edit needs more context, or a line number. |
| `CONVERSATION_NOT_FOUND` | The conversation does not exist, or it belongs to another tenant. The two answers are the same. | Check the conversation id and the tenant header. |
| `AUTH_REQUIRED` | The request carried no bearer token, or the scheme was not `bearer`. | Send the token in the `Authorization` header. |
| `ACCESS_TOKEN_EXPIRED` | The access token is past its expiry. | Refresh it. The browser does this on its own. |
| `TOKEN_REVOKED` | The token was revoked, or the user's token version moved on. | Sign in again. |
| `SESSION_REVOKED` | The sign-in session the token names was revoked. | Sign in again. |
| `TENANT_SCOPE_MISMATCH` | The `X-Tenant-Id` header does not match the tenant in the token. | Remove the header, or use the right token. |
| `RATE_LIMIT_EXCEEDED` | The request went past the limit. The DeepSpace chat budget is `rate_limit_queries_per_user_per_minute`, which is 60. | Wait, or raise the limit. |
| `DEEPSPACE_QUEUE_FULL` | The conversation already holds 100 queued turns. | Clear the queue, or let it drain. |
| `STORAGE_QUOTA_EXCEEDED` | The account's storage plan is full. The free plan holds 500 MB and the editor plan 1 GB. | Remove files, or change the plan. |
| `SERVICE_OVERLOAD` | The server is shedding load. | Wait and retry. |
| `DATABASE_NOT_READY` or `REDIS_NOT_READY` | A dependency of the API is not answering. | Check the database and the cache. |

### The limits of the local machine

- The SQLite database grows until the disk is full. No code path trims it.
  The retention and storage cleanup tasks exist, but the retention job
  deletes on a schedule and only for what a policy names.
- The account storage plan caps the object storage. The free plan holds
  500 MB and the editor plan 1 GB, counted across the documents, the
  Library files, the artifacts, and the pending uploads.
- One upload may be at most 26 214 400 bytes, which is 25 MB. This is
  `upload_max_bytes`.
- The log directory holds at most 6 files of 10 MiB each, so about 60 MiB.
- The workspace directory has no cap in the code. The agent's file tools
  and the shell commands write into it.
- The model directory holds the local models. In this checkout the reranking
  models take 2.4 GB, the text-to-speech models 367 MB, and the
  speech-to-text models 75 MB.
- The code sets no memory limit. The local stack runs the API, the Next.js
  development server, and the model work in one machine, and the Next.js
  development server is usually the largest of them. A deployment that
  runs a local model provider needs room for the model as well.
- The number of live terminal sessions is capped at 32 for the deployment
  and 4 per conversation, so a local machine is not asked to hold more than
  that.

## What to watch for

- The local ports and the Docker ports are different. A development proxy
  that points at 10000 works locally and misses the Docker API, which
  answers on 1000 behind the gateway.
- The readiness check does not test a model provider. A green check with a
  failing provider shows up as a failed turn, not as an unhealthy service.
- The local stack has no separate worker, so a long background job competes
  with the API for the same process.
- `scripts/start_scheduler.sh` starts `python -m app.deepspace.workers.daemon`
  when `AKS_DEEPSPACE_PROACTIVE_DAEMON_ENABLED` is `true`, and the daemon
  runs the same timed work the beat process runs. It holds an exclusive
  lock, so a second daemon refuses to start instead of sending the work
  twice, and it stops on `SIGTERM`.
- The gateway forwards the streaming routes with a different policy from
  the other API routes, so a proxy in front of the gateway needs the same
  care about buffering.
- The CORS list names the origins the API accepts. A new frontend port has
  to be added there, or the browser refuses the call.
- The log file is JSON, one object per line. `jq` reads it well:
  `jq 'select(.module=="app.deepspace.services.chat_service")' ~/.averqel-harness/logs/backend.log`.
- The `trace_id` field ties one request's lines together across the API
  and the worker.
- The `docker-compose.prod.yml` file the deploy script expects is
  `backend/docker-compose.prod.yml`. Every value the deployment may change
  has a default in it, so `docker compose -f docker-compose.prod.yml
  config` succeeds without an environment file. The real values come from
  the environment file the deploy script validates first.
- The database-backed tests need Docker. `./backend/scripts/test-isolated.sh`
  creates disposable PostgreSQL, Redis, and MinIO services on a temporary
  network, and removes them when it exits. The tests never touch a
  development, staging, or production database.
- The live provider proofs use the same disposable services with the test
  network namespace attached to a bridge, so the container can reach the
  provider. They skip without `DEEPSEEK_API_KEY`; the other variables name the
  endpoint and the two models, and `DEEPSEEK_BASE_URL` must point at an
  OpenAI-compatible `/v1` endpoint. From the repository root, run:

  ```sh
  export DEEPSEEK_API_KEY=...
  export DEEPSEEK_BASE_URL=https://larprouter.com/v1
  export DEEPSEEK_MODEL=deepseek-v4.1-flash
  export DEEPSEEK_SECOND_MODEL=glm-5.3-flash
  ./backend/scripts/test-live.sh "tests/integration/test_deepspace_live_provider.py tests/integration/test_deepspace_teammate_live_provider.py"
  ```

### DeepSeek thinking and tool calls

DeepSeek thinking responses can include an opaque `reasoning_content` value
that must be sent unchanged with the next assistant tool-call message. The
provider adapter captures that value from the stream, keeps it out of
user-visible history, encrypts it in the assistant message metadata, and
restores it before the next model request. This also applies when a DeepSeek
model is routed through an OpenAI-compatible provider.

The UI's Off, Low, Medium, High, Very High, and Extreme High choices are
translated into the provider's accepted DeepSeek request controls. Off sends
`thinking: disabled` and `reasoning_effort: none`; enabled choices send
`thinking: enabled` with the mapped provider effort. When thinking is off,
previous private reasoning is removed from the request.

If an old or incomplete tool turn has no recoverable continuation, DeepSpace
does not send the malformed assistant/tool pair to DeepSeek. It converts that
pair into quoted historical context and asks the model to continue safely.
This prevents the DeepSeek 400 error about missing `reasoning_content` and
avoids repeating a completed side effect.
