# 24. Operations and troubleshooting

This chapter covers the local Electron application and development runtime:
how to start it, where it stores data and logs, which local ports it uses,
and how to diagnose common failures.

## Use case

- An operator has to start the product and know that it is up.
- A developer has to find the reason a request failed from the logs.
- Someone has to know which port each service answers on before they
  change one.
- A team has to know how much disk and memory the local installation uses
  before they put it on a laptop.

## Why it matters

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
- The packaged app does not require containers or a separately managed
  backend. The API supervises the optional local SearXNG process when web
  search is enabled; its first-run installation is described in
  [chapter 41](41-local-searxng.md).

The script frees ports 10000 and 10030 before it starts, waits for the API
to answer, and then starts the web app. It prints the three addresses it
is serving: the web app on `http://localhost:10030`, the API on
`http://127.0.0.1:10000/api/v1`, and the API's own documentation on
`http://127.0.0.1:10000/docs`.

The API itself is started with `python run_backend.py` from `backend/`. It
reads `PORT` or `BACKEND_PORT` and defaults to 10000. It binds
`127.0.0.1`, and it reloads on a source change unless `RELOAD=false`.
During source reloads, Uvicorn drains existing requests and WebSockets for up
to 10 seconds before cancelling remaining connections, so a long-lived
session cannot leave the development API stuck in shutdown. An active chat
may need to reconnect after a backend source change. This limit
does not apply when reload is disabled or to the separately supervised
packaged-backend lifecycle.

### DeepSpace runtime status

The DeepSpace Runtime settings tab reads the current backend process through
`GET /api/v1/deepspace/sandbox/runtime`. It refreshes on tab entry, every 30
seconds while visible, when the app window regains focus, and on the Refresh
button. `observed_at` is the backend response time. A missing status field is
shown as `Not reported`; this commonly means the backend is older than the
frontend. Restart the local backend after updating it to load the current
status contract.

The panel is diagnostic, not a live editor for deployment environment
settings. Environment changes take effect after the backend process restarts.
Bubblewrap and macOS Seatbelt availability probes are cached for that process
lifetime. Read-only and Workspace shell commands require a full filesystem and
network boundary and fail closed when the host only reports partial isolation;
Full access uses the local OS account's permissions outside that cage.

The `deepspace_shell_enabled` and `deepspace_terminal_enabled` flags are
separate. The first controls one-shot `bash`/`pwsh` calls; the second controls
persistent terminal tools and workspace terminal routes. When terminal
sessions are disabled, the API refuses to open or use a session, while the
close route remains available for cleanup.

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

### The ports

| Port | What answers there | Where it is used |
|---|---|---|
| 10000 | The API, under `/api/v1`. | The local stack. |
| 10030 | The web app. | The local stack. |
| 8888 | The supervised local SearXNG process. | The desktop app when web search is enabled. |
| 11434 | Ollama, when the deployment runs a local model. | A local model provider. |
| 8000 | vLLM, when the deployment runs a local model. | A local model provider. |
| 1234 | LM Studio, when the deployment runs a local model. | A local model provider. |

The CORS list in the settings names the browser origins the API accepts:
`http://localhost:10030`, `http://127.0.0.1:10030`, `http://localhost:1030`,
`http://127.0.0.1:1030`, `http://localhost:3000`, `http://127.0.0.1:3000`,
`http://localhost:5173`, and `http://127.0.0.1:5173`.

Metrics availability is controlled by backend configuration; no external
metrics collector is required by the desktop runtime.

### The processes

| Process | Started by | What it owns |
|---|---|---|
| The API | `run_backend.py` or the packaged Electron backend supervisor. | The HTTP routes, streaming responses, approvals, and model requests. |
| Local task execution | The API's embedded in-memory Redis and eager Celery configuration. | Background tasks run in-process without a separate worker service. |
| The web app | `pnpm dev` or `pnpm start`. | The interface, and the browser's calls to the API. |
| The Electron shell | `pnpm --dir applications/desktop dev`. | The desktop window. |
| Local SearXNG | `run_backend.py`, when enabled. | The loopback-only web-search endpoint at `127.0.0.1:8888`. |

In the local app there is no separate worker. `redis_url` defaults to
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

The packaged backend writes its structured log to the AverQel home and also
prints to standard output, which Electron captures for diagnostics.

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
| `models/` | The local embedding and reranking models. |

Standalone Kinesis uses `~/Kinesis` itself as the default project workspace.
Other local runtimes use
`<AVERQEL_HOME>/workspaces/<tenant id>/<user id>`. Shared deployments and
explicit workspace roots retain tenant/user-scoped paths. Desktop named
workspace registrations live under `<AVERQEL_HOME>/workspaces`; explicitly
selected project folders remain at their original paths.

### When Full access says a host file is unavailable

Read the conversation's `GET /api/v1/deepspace/chats/{id}/sandbox-mode`
response and check `effective_mode`. A selected Full access value can be
narrowed by the active preset or deployment maximum. If the effective mode is
Full access, wildcard searches still default to the selected workspace. For
one exact filename, one `fs_glob` call with no `path` searches the workspace
and the current user's home, then `/mnt` and `/media` as the bounded budget
allows; it stops at the first match. The result's `search_roots`,
`scanned_entries`, `search_truncated`, and `skipped_paths` show what the tool
actually checked. A no-match result with `search_truncated=true` is incomplete,
not proof that the file does not exist. Each walk inspects at most 10,000
filesystem entries, and DeepSpace permits at most four `fs_glob`/`fs_grep`
calls per turn, even across approval pauses. The model should report the
permission or search-budget limitation rather than repeat shell `find`/`grep`
commands. Full access uses the backend process's OS account and cannot bypass
that account's filesystem permissions. Do not start at `/` unless the user
explicitly requests an entire-system search.

When a host search reaches a protected Linux tree such as `/proc`, `/sys`,
`/dev`, `/run`, `/boot`, or `/root`, the file tool skips that tree and reports
it under `skipped_paths`. When a normal folder is unreadable, the tool skips
that folder and continues. This is expected read-permission behavior and does
not require running DeepSpace with `sudo`. If the result contains matches,
they are valid even when `skipped_paths` is non-empty; inspect the reported
paths when the user expected files from a particular location.

### When a file reader is unavailable

`fs_read` handles text directly and sends supported document, image, PDF, and
Office files through the shared ingestion extractor. Inspect
`averqel_inspect_query` with `capability="files"` to see the exact supported
extensions, reader methods, byte/page/text limits, OCR/vision state, and
whether LibreOffice is installed for conversion-backed formats.

If the result contains `capability_failure=true` or `stop_turn=true`, it is a
terminal capability result for that turn. DeepSpace reports the error code,
format, searched path, and next safe option; it does not retry the same work
with `bash`, `find`, `sandbox_execute`, browser navigation, or guessed tools.
Unsupported proprietary formats, missing conversion binaries, malformed files,
permission failures, and size limits must be imported/uploaded, converted by
the user, or handled after the deployment adds the reported capability. A
successful extraction is not proof of perfect visual fidelity; its
`extraction_method`, `coverage_score`, OCR/vision flags, and warnings are the
evidence the operator should report.

The Codex CLI provider runs inference inside a short-lived `/tmp` directory.
That directory is private scratch space, not the conversation workspace. Use
the selected workspace route or the `Sandbox mode` and workspace instructions
included with the turn to identify the actual boundary.

### Browser and computer automation availability

`GET /api/v1/deepspace/sandbox/runtime` reports `browser_automation.available`
and `computer_use.available`, with a `reason` when either feature cannot run.
The desktop build includes the Playwright Python package and Chromium. Linux
`.deb` and `.rpm` installers include the local computer driver's `xdotool`
and `scrot` prerequisites; AppImage deployments need these on the host. The
local driver requires `DISPLAY` and an X11 or XWayland session. Tools are not
offered to the model while their feature is disabled or prerequisites are
missing.

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
  models take 2.4 GB.
- The code sets no memory limit. The local stack runs the API, the Next.js
  development server, and the model work in one machine, and the Next.js
  development server is usually the largest of them. A deployment that
  runs a local model provider needs room for the model as well.
- The number of live terminal sessions is capped at 32 for the deployment
  and 4 per conversation, so a local machine is not asked to hold more than
  that.

## What to watch for

- A clarification can wait while the user is away. **Cancel and continue without MCP** resumes the task; **Stop this task** cancels the paused run. A resumed turn's elapsed timer starts when the user answers, not when the older assistant message was first created.
- The readiness check does not test a model provider. A green check with a
  failing provider shows up as a failed turn, not as an unhealthy service.
- The local stack has no separate worker, so a long background job competes
  with the API for the same process.
- `scripts/start_scheduler.sh` starts `python -m app.deepspace.workers.daemon`
  when `AKS_DEEPSPACE_PROACTIVE_DAEMON_ENABLED` is `true`, and the daemon
  runs the same timed work the beat process runs. It holds an exclusive
  lock, so a second daemon refuses to start instead of sending the work
  twice, and it stops on `SIGTERM`.
- The CORS list names the origins the API accepts. A new frontend port has
  to be added there, or the browser refuses the call.
- The log file is JSON, one object per line. `jq` reads it well:
  `jq 'select(.module=="app.deepspace.services.chat_service")' ~/.averqel-harness/logs/backend.log`.
- The source-only CI test path is `cd backend && pytest -m unit_no_db`.
  Database-backed tests require explicitly disposable local PostgreSQL,
  Redis, and MinIO test services; the desktop app and test runner do not start
  those services.

### Tool schemas and unavailable-tool errors

DeepSpace sends the native schemas allowed by the current preset and plan or
task stage directly to the provider. If an authorized native tool is missing,
inspect the provider request's tool names and check deployment availability,
preset allowlists, plan mode, task stage, plugin state, extension rules, and
provider tool-call support. A supplied schema still does not bypass current
sandbox, approval, hook, or tenant checks. `discover_tools` is no longer sent
to the model; one legacy attempt receives a direct-use correction, and a
repeated attempt is stopped to prevent a trajectory loop. MCP tools continue
to use their separate live connection-scoped broker.

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

### Codex tool calling and reasoning visibility

DeepSpace shows the reasoning the provider exposes, not hidden chain-of-thought.
OpenAI does not return raw internal reasoning tokens to a client. DeepSpace
asks Codex for the `auto` reasoning summary on every turn, because the model
catalog otherwise defaults to no summary. Token counts are shown only when the
selected Codex path returns usage data.

- In standalone Kinesis mode, the backend provider checks the signed-in local
  Codex CLI account and invokes its restricted app-server directly. The CLI
  app-server receives DeepSpace's function tools and returns tool calls to
  DeepSpace's existing policy and execution loop. Tool calls therefore appear
  as normal tool activity and remain subject to DeepSpace's tool policies. The
  backend launches `codex app-server`; it does not use the unrestricted
  `codex exec` path or load CLI tokens into Python. The desktop provider form
  reads the local CLI status and models from the backend. If the CLI is absent
  or its session cannot be used, the same panel can start ChatGPT browser sign-in.
  Electron only opens the authorization URL; the backend receives the fixed
  loopback callback, validates state/nonce/PKCE and the ID token, and stores the
  account encrypted at `<AVERQEL_HOME>/secrets/openai-codex-accounts.enc`.
- Check `/providers/codex-local/status` to see whether the CLI and ChatGPT
  sign-in are available. For CLI setup, confirm the backend runs under the same
  OS user that ran `codex login` and refresh the provider models. For browser
  sign-in, check that the local backend stays running while the browser returns
  to its loopback callback. OAuth tokens, model discovery, refresh, revocation,
  and Responses API streaming are owned by the backend; these sign-in accounts
  are for standalone local Kinesis mode.
- The backend app-server adapter forwards
  `item/reasoning/summaryTextDelta` and `item/reasoning/textDelta`
  notifications as they arrive. The Internal Thought entry streams the summary
  live. Browser OAuth streams Responses API events and is considered successful
  only after `response.completed`; an interrupted or incomplete stream is an
  error, not a completed answer.
- If the provider returns no summary text, DeepSpace shows the
  `Reasoning completed (N reasoning tokens)` note so the turn still reports
  that reasoning happened. That note is a count, not hidden reasoning.

If the Internal Thought entry is collapsed, open it from its header. Completed
entries collapse automatically, but their full reasoning text remains in the
scrollable detail panel.
