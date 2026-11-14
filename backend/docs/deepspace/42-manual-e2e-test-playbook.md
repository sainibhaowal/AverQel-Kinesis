# 42. Manual end-to-end test playbook

This playbook gives an operator a repeatable way to exercise DeepSpace through
the local app, from model tool selection through the final saved result. It
covers the 15 capability areas requested for acceptance testing and adds
checks for the other documented DeepSpace surfaces. It is a procedure for
collecting evidence; it is not a claim that every provider, model, operating
system, connector, or optional runtime has been tested.

## Use case

Use this chapter when you want to check whether the app can perform real work
through tools, coordinate agents, preserve its history, enforce its boundaries,
and show the result in the interface. Run the cases with the provider and
host you intend to use, and record unavailable prerequisites separately from
product failures.

## Why it matters

- A final answer can say a tool ran even when no tool-call event exists. The
  trajectory, tool results, and the expected change to a disposable fixture
  are the evidence.
- A model may not support tool calls, parallel calls, image input, or computer
  use. A successful ordinary chat does not prove those capabilities.
- Several features are service-only, optional, or explicitly incomplete in
  the current web interface. A missing UI control must not be reported as a
  successful end-to-end path.
- Testing against a daily profile, a real workspace, a live connector, or a
  remote host can change data outside the test. This procedure uses a
  separate local profile and harmless fixtures.

## How it works

### Result labels

Use one label for every case and every repeat.

| Label | Use it when |
|---|---|
| **PASS** | The configured feature completed and all required evidence and data checks match. |
| **FAIL** | A configured and available feature violated the documented behavior, lost or duplicated work, returned a wrong result, or crossed a boundary. |
| **BLOCKED** | A required provider, model capability, OS backend, executable, service, credential, or UI integration is not configured or reachable. Record the exact prerequisite. |
| **INCONCLUSIVE** | The run finished, but the available event, timing, or output evidence cannot establish the expected result. |
| **NOT APPLICABLE** | The deployment intentionally does not offer that capability. This is not a pass. |

An expected policy refusal can be a **PASS** for a negative security case. A
tool refusal caused by missing configuration is **BLOCKED**. Keep those two
outcomes distinct.

### Safe test profile

Run the development Electron launcher with a separate data directory and
unused loopback ports. Do not use `start-local.sh` for this setup: its normal
startup frees its fixed ports. Choose other free ports if 11200 or 11230 are
already occupied; do not stop the process that owns them.

From the repository root on Linux, start a disposable profile:

```bash
TEST_PROFILE="$(mktemp -d /tmp/averqel-deepspace-e2e.XXXXXX)"
export AVERQEL_HARNESS_HOME="$TEST_PROFILE"
export AVERQEL_HARNESS_BACKEND_PORT=11200
export AVERQEL_HARNESS_FRONTEND_PORT=11230
export AKS_DATABASE_URL="sqlite:///$TEST_PROFILE/db/knowledge.db"
export AKS_REDIS_URL="memory://"
export AKS_STORAGE_BACKEND=local
export AKS_LOCAL_STORAGE_PATH="$TEST_PROFILE/storage"
export AKS_DEEPSPACE_WORKSPACE_ROOT="$TEST_PROFILE/workspaces"
export AKS_PROVIDER_SECRET_BACKEND=env_keyring
export AKS_LLM_PROVIDER=disabled
unset AKS_LLM_API_KEY AKS_PROVIDER_SECRET_ACTIVE_KID AKS_PROVIDER_SECRET_KEYRING_JSON
export HF_HOME="$TEST_PROFILE/cache/huggingface"
export HF_HUB_CACHE="$TEST_PROFILE/cache/huggingface/hub"
export AKS_DEEPSPACE_APPROVAL_MODE=ask
export AKS_DEEPSPACE_SANDBOX_REQUIRED=true
export AKS_DEEPSPACE_SANDBOX_NETWORK=false
export AKS_SEARXNG_LOCAL_RUNTIME_ENABLED=false
node scripts/electron.mjs dev
```

The launcher maps `AVERQEL_HARNESS_HOME` to `AVERQEL_HOME`. The explicit
database, cache, storage, and workspace overrides keep inherited operator
settings from redirecting this run to the regular local services or files.
Before testing, verify those exported values point under `TEST_PROFILE` and
the database URL begins with `sqlite:///`; stop if any database, cache,
storage, or workspace root points elsewhere. Electron profile, logs, and
local secrets also live under
the new directory. `ask` makes destructive tool calls pause for a decision, and
`sandbox_required=true` makes shell and extension-code tests fail closed when
the host has no supported cage. `sandbox_network=false` is enforced only by
backends that support network isolation. Keep this test window open while
running UI cases and stop this launcher with Ctrl+C when the run is complete.

This profile isolates Kinesis data and ports. The launcher inherits the
invoking environment, and the backend runs with the current OS user's rights;
the profile is not a separate OS account or a general-purpose container. Use
only a test provider credential you are authorized to use, and do not copy
secrets from the main profile into screenshots, prompts, or evidence. The
isolated app will make a new provider-encryption key under its own profile;
add a test provider in that app. Do not run untrusted hooks, plugin code,
shell commands, browser automation, or computer-use actions in this profile.

If the main app already supervises SearXNG on port 8888, keep the test
profile's local SearXNG supervisor disabled as above. The search case later
can use a separately configured test endpoint or be marked **BLOCKED**; do
not start a second process on the same port. The isolated test runner must
not use the regular development database, cache, object store, or a live
connector.

### Configure the test account and fixture workspace

In the test app, add a provider and select a model that supports function
tool calls. Verify the model is reachable with one short text-only turn before
starting the feature cases. Record the provider and model identifiers, not
their credentials. If the selected model cannot call tools, tool-dependent
cases are **BLOCKED**.

Create a folder named `e2e-workspace` under `TEST_PROFILE` and select it from
the app's workspace menu. The following fixture can be created from a shell
opened in that test folder, or with the app's workspace file tools. If you
use the shell, enter only these paths under the disposable profile:

```bash
mkdir -p e2e-fixture/site e2e-fixture/notes
cat > e2e-fixture/README.md <<'EOF'
DeepSpace manual E2E fixture.
The CSV has 3 data rows. Expected amount total: 50.
Group A total: 35. Group B total: 15.
EOF
cat > e2e-fixture/amounts.csv <<'EOF'
id,amount,group
1,10,A
2,15,B
3,25,A
EOF
cat > e2e-fixture/site/index.html <<'EOF'
<!doctype html>
<html><head><meta charset="utf-8"><title>DeepSpace E2E fixture</title></head>
<body>
  <h1>DEEPSPACE_FIXTURE_HOME</h1>
  <a href="/second.html">Open second fixture page</a>
  <form id="fixture-form">
    <label>Test value <input name="value"></label>
    <button type="submit">Show marker</button>
  </form>
  <output id="result">READY</output>
  <script>
    document.querySelector("#fixture-form").addEventListener("submit", (event) => {
      event.preventDefault();
      document.querySelector("#result").textContent =
        "E2E_OK:" + document.querySelector("[name=value]").value;
    });
  </script>
</body></html>
EOF
cat > e2e-fixture/site/second.html <<'EOF'
<!doctype html><html><head><title>Second E2E page</title></head>
<body><h1>DEEPSPACE_FIXTURE_SECOND</h1><a href="/index.html">Home</a></body></html>
EOF
```

The terminal inherits a reduced environment. Use the workspace-relative
commands above from the selected workspace; do not rely on shell variables
from the launcher being present inside a terminal session. Keep the fixture
free of real documents, credentials, and personal data.

### Evidence to collect on every case

For each repeat, record the case ID, conversation ID in your private ledger,
provider/model, agent mode, sandbox mode, workspace, start/end time, outcome,
tool-call and tool-result names/statuses, step count, final turn status, and
any error code. Save a screenshot only when it shows a UI behavior that a
trajectory cannot show. Keep logs and screenshots inside the test profile;
they may contain prompt text, paths, and tool arguments.

Use the **Trajectory** panel and the session trace to confirm actual
`step/start`, `tool/call`, `tool/result`, `step/end`, and final assistant
events. Read the actual outcome from the stream's `done` frame or persisted
run record; the `turn/end` event currently records `status: completed` even
when the run outcome was blocked, cancelled, or failed. For delegated work,
look for child IDs and child tool events; for teams, also inspect member,
message, and task events. A green health check proves the API, database, and
cache are reachable; it does not prove the provider works.

### Repeat plan and measurement rules

Run each enabled case three times: one fresh conversation and two new turns
in separate conversations using the same fixture. Run timing-sensitive
parallel cases five times. Do not reuse a conversation if its goal, queue,
team, terminal, schedule, or browser session can affect the next trial.

Measure end-to-end turn time, time to first visible assistant text, provider
latency when available, each tool's duration, tool calls attempted/completed/
failed/refused, model rounds, and final status. For parallel work, also record
the number of independent calls in one model step and whether their execution
intervals overlap. Report p50 and p95 only when there are enough observations;
do not treat three samples as a stable service-level estimate. The manual
does not define a universal latency target. Compare runs using the same model,
prompt, fixture, and host.

For normal reliability, require 3/3 correct outputs and no lost or duplicate
mutations. For concurrency claims, require same-step independent read calls
and overlapping execution intervals; a short total time by itself is not
proof. If the trace does not include usable timing, mark concurrency
**INCONCLUSIVE**. For security cases, any unexpected successful access or
write outside the disposable workspace is an immediate **FAIL** and a stop
condition; preserve the test evidence and do not continue with broader
permissions.

### S01 — Direct native schemas and tool execution

**Covers request 1 and chapters 09–11.** This verifies model-to-registry
tool calling, preset-filtered upfront schemas, argument validation, and the
user-visible tool/result cards.

In a fresh conversation, select **Standard** and the fixture workspace. Send:

> Use the supplied tools to list workspace paths and read a text file. Read
> `e2e-fixture/README.md` and `e2e-fixture/amounts.csv`, then report the row
> count and the expected total from the README. Do not change any file.

**PASS evidence:** the first provider request contains the exact read schemas
allowed by the selected preset, contains no `discover_tools` schema, and the
model calls the supplied tools successfully. Record completed read results,
the correct values, and matching visible tool/result entries. If the model
only describes a tool call in text, it did not pass.

Repeat in **Minimal**, **Creator**, **PTC**, and a configured **Custom** preset.
For each one, verify the request contains its effective allowed schema set and
excludes tools withheld by that preset. Then make one deliberately repeated
legacy `discover_tools` attempt through a controlled test model: the backend
must correct the first attempt, stop on the second, and execute no discovery
action. Do not run this negative case against real workspace data.

Then make a read-only negative check by asking for a deliberately nonexistent
fixture path. The tool should return a bounded not-found result without
crashing the turn or reading a path outside the workspace. Record the exact
refusal; do not ask it to search your real home directory.

### S02 — Multiple tool calls and parallel native reads

**Covers request 2 and chapter 03.** First send:

> Inspect `e2e-fixture/README.md`, `e2e-fixture/amounts.csv`, and
> `e2e-fixture/site/index.html` independently. Use read-only workspace tools
> and request the independent reads together in one model round. Summarize
> the expected totals and the page title. Make no changes.

The provider/model must support multiple function calls in one response.
Inspect the trace for at least two native `read` calls in the same `step`.
Compare their start/end times with each other and with the total step time.
Repeat five times.

**PASS evidence:** all fixture values are correct; calls are not lost or
duplicated; independent reads in the same batch overlap; the results return
to the model. A serialized MCP request is expected by design and is not
evidence of native-read parallelism. A model that makes one call per round
has not demonstrated parallel tool calling; mark **INCONCLUSIVE** if the
model lacks parallel-call support or the trace lacks timing evidence.

Optional ordering check: in a new fixture conversation, ask for a write to
`e2e-fixture/order.txt` followed by a read of that path. Confirm the read
observes the completed write. Do this only in the disposable workspace and
with approval enabled.

### S03 — Agentic loop, plan, and final answer

**Covers request 3 and chapters 02, 19, 21, and 29.** Use a fresh conversation
in **Standard** mode and send:

> Solve this from the fixture, not from memory: inspect the CSV, calculate
> the total and per-group totals, verify your calculation with a second
> independent method using a tool, write the result to
> `e2e-fixture/report.md`, read it back, and present it as a deliverable.
> Work only in this workspace. State the checks you actually performed.

**PASS evidence:** the trace contains more than one provider step, completed
reads, an allowed write, a completed read-back, and the final answer agrees
with 3 rows, total 50, group A 35, group B 15. The report exists and
`present` names that existing workspace-relative file. Check **Changed
Files** and **Deliverables** for the report. Any write pause must be approved
or denied explicitly. A correct-looking answer without the required events
and file is a failure of the requested end-to-end path.

Run a second fresh conversation in **Plan mode**. Ask it to propose this
same fixture audit and report. Verify it can read but does not write before
the plan review; approve the plan only after confirming it names the fixture
and checks. Verify work starts after approval. Reject or revise a separate
plan once and confirm that rejected work did not start.

### S04 — One-shot and continuable child agents

**Covers request 4 and chapter 04.** Send:

> Delegate two independent read-only checks of the fixture: one child should
> verify the CSV row count and totals, and one should inspect the HTML title
> and fixture markers. Give each child the full task text and restrict its
> tools to workspace reads/searches. Wait for both results, inspect their
> statuses, and combine their evidence. Do not let a child write files.

Inspect the agent tree and trace for `subagent/spawn`, descriptor, child
tool-call/result events with child IDs, and completed child results. Compare
each child's answer with the fixture. Then send one follow-up to a
continuable child using `send_message` and read the answer with `get_agent`;
the send response itself is not the answer.

**PASS evidence:** each child completed its assigned independent check, the
tool filter prevented writes, the parent synthesized both results correctly,
and the follow-up belongs to the same continuable child. If the deployment
or selected agent mode withholds delegation, record **BLOCKED** with that
policy evidence.

### S05 — Parallel child work and nested delegation

**Covers request 5, parts of request 6, and chapters 03, 04, and 38.** This
case tests *agent-turn* concurrency, which differs from parallel native
reads. First discover whether `workflow_run`, the Node PTC runtime, and the
OS cage are available. The Node PTC runtime is off by default in the documented
configuration and the workflow path requires Node.js and bubblewrap; do not
enable it in the main profile.

When available, run a workflow in the test workspace with three independent
read-only fixture tasks and a final synthesis step. Limit its concurrency to
three; each child must receive only the relevant fixture paths and must not
write. Inspect the **Workflows** panel and trajectory, not just the final
summary. Repeat five times and record provider-turn start/end intervals for
each child. Host tool execution may still be serialized for database safety,
so distinguish overlapping model turns from overlapping tool handlers.

Then, if the configured delegation depth is at least two, ask one child to
delegate one read-only grandchild check. Confirm the child IDs show the
expected depth and the configured maximum is respected. Do not try to exceed
the configured depth or child count as a stress test on the active profile.

**PASS evidence:** all three child results arrive exactly once, each checks a
different fixture, the synthesis matches the known values, and measured child
turn intervals overlap when concurrency is claimed. If workflow is disabled,
Node PTC or `bwrap` is absent, record **BLOCKED** and still report ordinary
subagent behavior from S04 separately.

Then exercise the parts of the workflow contract that S05's first run does
not reach, using the same disposable profile:

- Ask for one child that must answer with a schema, for example an object
  with a `score` integer and a `verdict` string. Confirm the script's result
  holds the object, not a string of prose, and that a child that cannot match
  the schema yields no object rather than a wrong one.
- Ask for a per-item pipeline over three items and confirm one result per
  item, in the original order.
- Ask for a child with a `provider` override and confirm it runs under that
  provider in the run's own record.
- Watch the **Workflows** panel while the run is in flight: the phase, the
  log lines, and the agent count appear as they happen, without a manual
  refresh.
- Open the **Workflow Editor**, create a named workflow with one step, save
  it, and confirm it appears in the workspace under
  `.averqel/workflows/`. Open the **Workflows** panel and run that named
  workflow by name; it must complete once, without repeating itself.

**PASS evidence:** the schema answer is an object, the pipeline returns one
result per item, the provider override is recorded, the panel shows live
progress, and the named workflow completes exactly once.

### S06 — Named teammates, shared board, messages, and teammate children

**Covers request 6 and chapter 05.** In a new conversation, ask the parent
to create two named teammates with read-only tool filters, create one board
task per teammate, and then send each a task-specific message. Verify that
creating a teammate leaves it idle; it should start only after
`team_send_message`. Read the roster with `team_list_members`, the answers
with `team_get_member`, and the board with `team_task_list`.

If delegation depth permits, include `subagent` plus only the needed
read tools in one teammate's `tool_filter`, then ask that teammate to start
one read-only child. Confirm the delegation tree and that the child cannot
address the shared team board. Keep all agents read-only in this case.

Optional revision-race check: on a disposable board task, have two teammates
read the same revision and attempt distinct status updates. The stale update
must be rejected with the current revision; the losing teammate must reread
before updating. Do not blindly repeat a stale mutation. Record idempotency
keys for any retry where supported.

**PASS evidence:** two distinct teammates are created, messages start their
turns, both report correct independent results, board owners/statuses/revisions
are consistent, and no message or result is lost or duplicated. Check for
`team/member-spawned`, `team/message`, `team/task`, and member-state evidence.

### S07 — Computer-use provider on a virtual desktop

**Covers request 7 and chapter 27.** The documented computer-use path is a
VNC/Xvfb virtual display with `computer_*` tools; it is not the embedded
browser and it does not drive the user's physical desktop. The host needs a
reachable configured VNC/Xvfb runtime. The feature flag alone does not create
that runtime.

Use only a disposable Xvfb display with a harmless test window that you
control. Ask the agent to take a screenshot, read a clearly visible marker,
click a known control, type `E2E-CUA-OK`, and verify the new marker with
another screenshot or OCR. Do not open account settings, terminals with
credentials, file pickers, or your normal desktop in this session.

**PASS evidence:** the `computer_*` tools are actually offered, screenshot
and action calls complete, the fixture window changes as expected, and the
final screenshot/OCR confirms the change. If VNC, Xvfb, the target window, or
a computer-use-capable model is missing, record **BLOCKED**. The manual does
not treat the feature as hardened for untrusted execution.

### S08 — Hooks: allow, block, ask, and audit

**Covers request 8 and chapter 12.** Hooks are deployment commands, run
outside the tool cage. Configure them only in the disposable profile before
starting that backend. Use the manifest format in [chapter 12](12-hooks.md),
save it under `TEST_PROFILE`, and set
`AKS_DEEPSPACE_HOOK_MANIFEST` to that manifest's absolute path in the shell
that launches the test app. Use a small trusted local guard that watches
exactly `fs_write` and inspects only the fixture-relative path. It must never
log environment variables, credentials, or full prompt contents. The guard
is ordinary host code outside the sandbox, so use only a script you wrote or
reviewed.

1. A matching `PreToolUse` call for `e2e-fixture/blocked.txt` returns a
   `block` decision with a unique harmless reason. Ask DeepSpace to write
   that file. Confirm the hook result is visible and the file does not exist.
2. A call for a different fixture path returns `allow`. Confirm the tool
   runs and the file exists after any configured approval.
3. In a separate turn, return `ask`. Confirm the call pauses with an approval
   request carrying the hook question. Deny it and verify the file is absent;
   in a separate safe test, approve and verify one write occurs.
4. Inspect the trajectory for `hook/invoked` and `hook/result` for each
   actual invocation.

**Important documented limit:** `PreToolUse` is wired into the live tool
pipeline. `PostToolUse`, `Stop`, and `SessionStart` are supported by the hook
module and manual `hook_run`, but are not automatically invoked by turns in
this release. Do not count those as live lifecycle-hook passes. `hook_run`
itself is a read-only preview and does not block the subsequent tool call.

### S09 — Sandbox modes, backend proof, and policy refusals

**Covers request 9 and chapters 13 and 20.** Before running commands, read
the sandbox status at `GET /api/v1/deepspace/sandbox/backend` and runtime at
`GET /api/v1/deepspace/sandbox/runtime`, or use the equivalent runtime
inspection UI/tool. Record configured backend, selected backend,
enforcement level, `sandboxed`, and `failure_kind` from command results.

Run these only with the isolated profile, the selected fixture workspace,
and `sandbox_required=true`:

1. In `workspace-write`, create and read a marker file inside the fixture.
   This should complete and report the actual cage selected.
2. In `read-only`, request a harmless fixture write. It should be denied and
   leave the fixture unchanged. Confirm it is a policy denial, not a broken
   runner.
3. In `workspace-write`, attempt to write a sibling sentinel outside the
   selected workspace. Use a sentinel created solely for this test. It must
   remain byte-for-byte unchanged. Also test a workspace symlink that points
   to that sentinel; the file tools must refuse the path.
4. When the selected backend supports network isolation (`bwrap` on Linux or
   `sandbox-exec` on macOS), set the isolated profile's sandbox network
   switch off and attempt a connection to the local fixture server from a
   sandboxed command. It should fail to connect. Landlock and Windows ACL do
   not enforce the network switch; record **NOT APPLICABLE** for that claim.
5. If the host offers no cage, a command must be refused with
   `SANDBOX_UNAVAILABLE`; it must not silently report an uncontained command
   as sandboxed.

Never use `danger-full-access` for these boundary checks. If a result says
`sandboxed: false` when a cage was required, stop the suite. `denied` means a
rule did its job; `runner_failure` means the boundary itself failed and is a
test failure requiring investigation.

### S10 — Persistent terminal and reviewed remote runtime

**Covers request 10 and chapter 14.** In the test conversation, open one
local terminal in the fixture workspace. At its prompt run this bounded
POSIX-shell command:

```sh
printf 'TERMINAL_START\n'; IFS= read -r line; printf 'TERMINAL_RECEIVED:%s\n' "$line"
```

Read output, send `E2E_TERMINAL_OK` followed by a newline, read again, list
the session, then close it. Confirm the result contains the expected
`wait_reason`, session ID, and ordered output. Verify `terminal_close` leaves
no live test session. On Windows, use an equivalent PowerShell `Read-Host`
command and record that shell instead.

A persistent terminal is not proof that shell commands used the operating
system sandbox. Use S09's shell-tool result and its `sandboxed` and backend
fields to verify containment; keep terminal commands trusted and bounded.

Open a second terminal only if the per-conversation limit allows. Start the
local fixture HTTP server from that terminal with
`python3 -m http.server 18765 --bind 127.0.0.1 --directory e2e-fixture/site`
and use its loopback URL in S12 and S15. Stop it with `terminal_signal` or by
closing the terminal; verify the port is no longer serving the fixture.

For SSH, use only a disposable SSH server bound to loopback with generated
test-only host/client keys and a dedicated empty remote directory. Verify a
read, write, list, terminal or stream, and a path/symlink refusal inside that
directory. Confirm a changed host key is refused. Never point these cases at
your VPS or a real production host. `ssh_run` executes as the remote account
and is not limited to the reviewed file directory; do not use it for a broad
host-boundary test.

**PASS evidence:** local session output is ordered and persistent between
calls, session ownership is scoped to the test conversation, and close stops
the process. Remote runtime is **BLOCKED** if no reviewed loopback test host
is provisioned; that is not a reason to add a live host.

### S11 — Workspaces, files, notes, Library, and memory

**Covers request 11 and chapters 15 and 20.** Select the fixture workspace
and verify its name and path in the composer and workspace route. In separate
test conversations, create a harmless `workspace-marker.txt`, a Library
note, and one non-sensitive memory fact with a unique prefix such as
`E2E-2026-11-06`. Read each back using the tool schemas supplied for the
selected preset and verify the destination (workspace, Library, note, or
memory).

Check that the file panel shows the fixture file; that `fs_read`, `fs_glob`,
and `fs_grep` agree; that `workspace_diff` reports the expected changed path;
and that `present` accepts an existing in-workspace file. Verify a named
workspace is separate from the default workspace. Check that a second test
conversation cannot read the first conversation's Library item unless the
documented scope explicitly permits it. Memory is account-level, so verify
that distinction rather than expecting it to be conversation-private.

Negative path cases: ask for an absolute path, a `..` traversal, and a
symlink that resolves outside the fixture. Each must be refused without
crashing the turn or changing the sentinel. Exercise stale-version handling
only by reading a fixture file, changing it locally inside the fixture, then
asking for a version-guarded edit using the old version; expect
`FS_STALE_VERSION` and no lost update.

**PASS evidence:** all expected reads and writes use the selected workspace,
unrelated paths remain unchanged, version conflicts are surfaced, Library
and memory scopes match their documented ownership, and the file panel/diff/
deliverable views agree with the recorded changes.

### S12 — In-app browser panel

**Covers request 12 and chapter 16.** Start the fixture server from the
test terminal on `127.0.0.1:18765`. In the app's **Browser** panel, open
`http://127.0.0.1:18765/`. Verify the page title and
`DEEPSPACE_FIXTURE_HOME`; follow the fixture link, go back, go forward, and
reload. Use the panel's screenshot click and text controls on the local form
to submit `E2E_BROWSER_OK`, and verify the page displays
`E2E_OK:E2E_BROWSER_OK`.

Try a `javascript:` URL and confirm it is refused. Do not enable external
sites for this case. The embedded panel is a UI surface and is not the
`browser_*` agent tool surface; the fallback iframe does not prove a real
Chromium session. Record which browser mode the panel reports and whether a
Chromium runtime was available.

### S13 — Extensions, plugins, MCP tools/resources, and connectors

**Covers request 13 and chapters 17, 18, and 30.** Run independent subcases
with disposable local fixtures; do not connect personal accounts or live
work systems.

**Declarative extension/plugin:** install one harmless extension that adds
guidance and denies one named tool. Confirm it appears in the conversation's
extension/plugin inventory, takes effect at the documented next turn, and
cannot widen deployment policy. Remove it after the test. If testing code,
use only a tiny workspace-owned handler with no network, no credentials, and
the strictest working cage. Verify a timeout/crash is contained and recorded.

**MCP:** connect a local test MCP server that exposes one read-only echo tool
and one resource. Verify broker `search` → exact `schema` → `call`, then list
and read the resource. Confirm the call uses the returned `tool_ref`, a
blocked/read-only policy prevents an unauthorized write, and a conversation
override can keep the server out. A server with no resources should yield an
empty listing, not a false tool failure. Check MCP call and result budgets.

**Connector:** a connector is a scheduled ingestion feed, not an agent tool.
Only create one if you have a local stub or explicitly disposable connector
endpoint and test account. Sync one harmless fixture document, verify it is
searchable, and remove/disable the test connector afterward. If no safe
connector fixture exists, record **BLOCKED** and leave live connectors
untouched.

**Plugin HMR limitation:** chapter 30 documents the backend watcher/schema
service, while the current Settings page shows plugin inventory only. Do not
expect a configuration-schema form or HMR control in that page; test those
service methods only through a dedicated backend harness if required.

### S14 — Authentication boundary, presets, and permission behavior

**Covers request 14 and chapters 19–22 and 32.** In the local SQLite profile,
the app signs requests in as one local operator with all permissions. That
profile cannot prove cross-tenant isolation. It can prove per-conversation
mode and workspace behavior:

1. Confirm the effective model, agent mode, sandbox mode, and workspace in
   the UI and in their typed selection events.
2. Switch a test conversation through Standard, Minimal, Creator, PTC, and a
   saved Custom mode. Use `runtime_discover` before a call and verify the
   actual tool set: Minimal should not offer writes/shell/delegation; Creator
   should not offer shell/delegation; PTC requires its supported runtime and
   sandbox; Standard exposes tools only when deployment policy allows them.
3. Hide a non-default mode, verify it leaves the selector and cannot be
   recorded on a conversation, then restore it. Do not hide or change the
   deployment default just to force a test.
4. With `approval_mode=ask`, request one harmless fixture write and deny it;
   confirm no file appears. In another conversation approve one fixture
   write; confirm it occurs once. Never approve an action whose exact input
   is not understood.
5. Confirm secret values do not appear in shell/terminal environment,
   tool results, logs, screenshots, or event summaries. Do not print or
   export secrets to test this.

PermissionPresetService presets and auto-review are distinct from the
composer's agent modes. The documented Settings page does not edit those
service presets or auto-review. Test them only in an isolated backend
integration harness; do not claim the agent-mode selector tested them.

For tenant isolation, use only a separately provisioned, explicitly
disposable PostgreSQL/Redis/object-store environment and two separately
authenticated test users/tenants. The single-operator SQLite profile cannot
prove this boundary. Check that a foreign conversation, workspace, terminal,
MCP connection, and Library ID receive the same not-found response as a
nonexistent ID. If that isolated environment is not provisioned, record
**BLOCKED**; never simulate this using real accounts or the local operator.

### S15 — Playwright browser automation and computer-use distinction

**Covers request 15 and chapters 26–27.** This is separate from S07 and S12.
Use the fixture server from S10 only. Confirm the first provider request
contains the authorized `browser_*` schemas and Playwright and its browser are
available. Then navigate
to the local fixture, read the title, click the second-page link, return, fill the input,
submit, and capture a screenshot or page content showing
`E2E_OK:E2E_BROWSER_AUTOMATION_OK`. Close the browser session and verify its
tabs are gone.

The documented Playwright process runs outside the agent sandbox. Keep its
URL on loopback, do not sign into any account, and do not test downloads or
external sites in this suite. `computer_*` is a separate VNC/Xvfb virtual
desktop provider; use the S07 test window for that surface. Neither tool
family proves that the other works.

### S16 — Jobs, schedules, and prompt queue

**Additional coverage for chapter 06.** In a fresh test conversation, start
one bounded task that takes long enough to observe as running, then enqueue a
second harmless prompt. Verify queue order, one active turn at a time, and
that the second prompt is neither lost nor run twice. Read its job/output
state where the UI exposes it. Test edit, pause/resume, and cancellation only
on disposable queued prompts.

Create one test schedule at the documented minimum five-minute interval,
read it back, pause it before its next run if the scheduler is unavailable,
and delete it at the end. If you want to test a real scheduled run, confirm
the local scheduler/daemon is configured first and observe the complete run
record. A local desktop uses eager in-process Celery by default; it does not
prove a separate worker or production broker. A terminal background write
is an in-memory terminal job, not a durable conversation job.

**PASS evidence:** queue state transitions and ordering match the UI and
trace; the schedule is either observed running exactly once or correctly
reported **BLOCKED** because no scheduler is enabled; all temporary schedules
are paused or deleted.

### S17 — Workflow orchestration, goals, and loop protection

**Additional coverage for chapters 29, 38, and 39.** The workflow part uses
S05 prerequisites. The goal part uses a fresh conversation: discover
`goal_set`/`goal_read`, create one uniquely named goal limited to the fixture,
observe the goal bar and round count, then mark it complete and verify no more
continuation is queued. Confirm automatic continuation never exceeds its
configured round budget. Do not create an open-ended goal.

For the loop guard, use a disposable backend test configuration with a very
low identical-call limit and ask for a finite repeated read. For example,
before starting the disposable profile set
`AKS_DEEPSPACE_LOOP_GUARD_MAX_IDENTICAL_CALLS=2`; ask for the same harmless
fixture read three times and confirm the third identical call is refused with
`LOOP_GUARD_TRIPPED`, while the first two results remain available. Restore
the normal setting by discarding the test profile. Do not try to exhaust the
production/default limits.

Runtime invariants are diagnostic checks after a turn, not rollback or an
alerting service. A passing turn may emit no invariant frame. Verify their
reported status only when the isolated runtime exposes the result; do not
infer that every invariant ran from the absence of a warning.

### S18 — Trajectory, export, fork, feedback, and deliverables

**Additional coverage for chapters 07, 34–36, and 40.** Use a completed S03
test conversation.

- Open the **Trajectory** panel and compare the visible order with the typed
  session trace: turn, steps, tool calls/results, child events, final answer.
  Search and read one event; confirm search is a word match and results are
  bounded. Verify the trace remains readable after closing/reopening the
  test conversation.
- Export the test session as JSON/JSONL using the UI or documented export
  route. Inspect the exported payload's migration version and confirm export
  did not rewrite the original conversation. Do not run database migrations
  or edit SQLite rows manually.
- Fork at a completed fixture turn, verify earlier messages/events are copied
  through the selected point, the new conversation ID is different, and the
  fork has no running child agents. Use `copy_files` only inside the
  disposable profile.
- Use the feedback buttons on a test answer, then verify the saved feedback
  event/API response. Do not include real personal data in comments.
- Verify **Changed Files**, **Deliverables**, `workspace_diff`, and
  `present` agree for the test report. Terminal shell writes may not appear
  in the changed-file projection; declare a file with `present` when it is a
  deliverable.

**PASS evidence:** UI and typed-log order agree, exported data is versioned
without mutating source history, the fork diverges safely, feedback is saved
once, and deliverables name existing paths in the test workspace.

### S19 — Model selection, telemetry, and provider/runtime measurements

**Additional coverage for chapters 08, 22, and 28.** With two authorized
test models available, pin a test conversation to a known tool-capable model,
run S01, and confirm its `model/selection` event. In a separate conversation,
enable automatic selection and compare the recorded model and selection
reason. If only one model is available, manual selection can still be tested;
automatic model comparison is **BLOCKED**.

If telemetry is intentionally enabled in the test profile with a local
console or disposable OTLP collector, verify a session/turn/tool/provider
span hierarchy and check success, error, duration, provider/model, and token
fields where returned. Do not put prompts, secrets, or personal data into
span attributes. If telemetry is off, record **NOT APPLICABLE** rather than
turning on a production collector.

Record provider health and per-turn `TTFT`, total duration, tool durations,
tokens when available, and any `LLM_*` error code. Health readiness alone
does not test the model provider.

### S20 — Service-only surfaces: slash commands, permission presets, bundled
skills, and plugin configuration

**Additional coverage for chapters 30–33.** First verify the documented
integration boundary instead of assuming every service has a UI:

- The slash-command registry is not connected to the current composer/chat
  route. A `/help` message in the chat is not an end-to-end registry test.
  Test parse/execute/completion through a dedicated backend harness and check
  its documented result. Some built-in results are action payloads only and
  do not persist the action.
- PermissionPresetService presets and auto-review are separate from agent
  modes and are not edited in the web Settings page. Use a backend harness;
  verify approve/deny/timeout paths with a fake reviewer and fixture-only
  tools. Auto-review failure must deny the call, not silently approve it.
- Bundled skill definitions are not merged into `skill_list`/`skill_load`
  and are not auto-installed. Test listing/install only with a copied
  disposable workspace, then verify the expected Markdown file. Never run
  `install_all` over your normal workspace; the documented installer can
  overwrite same-named skill files.
- Plugin HMR schemas exist at service level; the Settings page currently
  shows inventory and does not render schema forms or direct HMR controls.
  Verify inventory separately from schema registration/validation.

Use the status **SERVICE-ONLY / NOT UI-WIRED** where that accurately describes
the result. It is useful evidence of the current boundary, not a failed
button click.

### S21 — Local web search and source citations

**Additional coverage for chapter 41 and chapters 09–10.** Check
`GET /api/v1/deepspace/web-search/status` and record `disabled`, `external`,
`not_installed`, `running`, or `failed`. When a test search endpoint is
available, issue one neutral, non-personal query. Do not point the test
profile at a SearXNG process already owned by the main app; use a separate
test endpoint or mark this case **BLOCKED**. Verify that `web_search`
returns URLs/titles/snippets, `url_read` reads a safe public or local fixture
page, the final answer cites only sources actually returned/read, and the UI
Sources drawer displays those citations and metadata.

Web search depends on internet access and public search engines; rate limits
or empty results are external availability conditions. The local SearXNG
supervisor's first installation downloads code and dependencies. Run that
first-install case only when you intentionally want to install it in the
test profile and have confirmed its configured port is free. Never stop the
main profile's search process to make this case pass.

### S22 — Headless CLI and `sdk-minimal` profile

**Additional coverage for chapter 37.** Run this only against the same
throwaway `AVERQEL_HOME` after stopping the isolated Electron launcher, so a
headless process cannot race the test app's SQLite database. Open a separate
shell and run from `backend/`:

The CLI creates a new conversation and does not inherit the UI conversation's
selected named workspace. The commands below create a separate harmless
fixture in this test profile's default workspace.

```bash
export AVERQEL_HOME="/absolute/path/to/test-profile"
export AKS_DATABASE_URL="sqlite:///$AVERQEL_HOME/db/knowledge.db"
export AKS_REDIS_URL="memory://"
export AKS_STORAGE_BACKEND=local
export AKS_LOCAL_STORAGE_PATH="$AVERQEL_HOME/storage"
export AKS_DEEPSPACE_WORKSPACE_ROOT="$AVERQEL_HOME/workspaces"
export AKS_PROVIDER_SECRET_BACKEND=env_keyring
export AKS_PROVIDER_SECRET_ACTIVE_KID=local
export AKS_PROVIDER_SECRET_KEYRING_JSON="$(< "$AVERQEL_HOME/secrets/provider-secret-keyring.json")"
export AKS_LLM_PROVIDER=disabled
unset AKS_LLM_API_KEY
TEST_TENANT_ID="00000000-0000-0000-0000-000000000001"
TEST_USER_ID="00000000-0000-0000-0000-000000000002"
TEST_MODEL_ID="<tool-capable-model-id>"
CLI_WORKSPACE="$AVERQEL_HOME/workspaces/$TEST_TENANT_ID/$TEST_USER_ID"
mkdir -p "$CLI_WORKSPACE/e2e-fixture"
cat > "$CLI_WORKSPACE/e2e-fixture/README.md" <<'EOF'
DeepSpace E2E fixture. Three CSV rows; expected total: 50.
EOF
cat > "$CLI_WORKSPACE/e2e-fixture/amounts.csv" <<'EOF'
id,amount,group
1,10,A
2,15,B
3,25,A
EOF
python -m app.deepspace.cli headless --tenant "$TEST_TENANT_ID" --user "$TEST_USER_ID" --model "$TEST_MODEL_ID" --format json "Read e2e-fixture/README.md and e2e-fixture/amounts.csv. Verify the total is 50 and report the row count."
```

The tenant and user IDs above are the local single-operator identity. For a
different test profile, use that profile's test identity. Replace the model
placeholder with the model ID chosen in the isolated app; do not paste API
credentials on the command line. The keyring assignment loads only the test
profile's generated encryption key into this CLI process; never print or
share it, and never substitute the main profile's keyring. Verify the JSON
contains a successful turn and the expected tool calls. If exercising the
`sdk-minimal` JSON-RPC profile,
send one initialize request, one turn request, and one shutdown request. For
example, this sends a bounded fixture read and lets you capture the
`tool/call` notification and final result:

```bash
cat <<EOF | python -m app.deepspace.cli headless --profile sdk-minimal --tenant "$TEST_TENANT_ID" --user "$TEST_USER_ID"
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"turn","params":{"prompt":"Read e2e-fixture/README.md and e2e-fixture/amounts.csv; verify the total is 50.","model":"$TEST_MODEL_ID"}}
{"jsonrpc":"2.0","id":3,"method":"shutdown","params":{}}
EOF
```

Do not run either CLI command against the default home while the normal app
is using its database, and do not rely on variables inherited by the app's
terminal; set the profile explicitly in the separate shell.

The headless runner uses the same database configuration as the server. A
successful CLI run is evidence for the headless path, not for the Electron UI
or its streaming/reconnect behavior.

### Compound acceptance run

After the individual cases pass, create one fresh compound conversation in
the test workspace. Keep its scope deliberately bounded. Use a
tool-capable model, Standard mode, `workspace-write`, and the `ask` approval
setting. Send:

> Work only in `e2e-fixture`. Use the authorized tools supplied for this preset.
> Read the README and CSV, and verify the three-row total using an independent
> method. Request independent reads together when your provider supports
> parallel calls. Ask two one-shot read-only children to independently
> verify separate fixture facts and wait for both. If the workflow runtime is
> available, run a three-item read-only workflow over the same fixture and
> synthesize its results. Create one `report.md` with the verified totals,
> checks, and any unavailable capability. Read the report back, present it as
> the deliverable, and finish with the evidence IDs for the trajectory. Do
> not access external sites, secrets, other workspaces, or remote hosts.

Do not force the compound prompt to test hooks, team writes, destructive
operations, or computer use. Those have their own controlled cases above.
Check that the parent did not claim an unavailable workflow ran, that the
children returned exactly once, and that the report, deliverable, diff, and
trajectory agree. Repeat the compound run three times in fresh conversations.

### Coverage map for the numbered manual

This map prevents the supplementary feature areas from disappearing behind
the 15 requested capability groups.

| Manual chapter | Case(s) |
|---|---|
| 01 What DeepSpace is | S01, S03, compound run |
| 02 The agentic loop | S03 |
| 03 Parallel execution | S02, S05 |
| 04 Delegation and subagents | S04, S05, S06 |
| 05 Teammates and swarm | S06 |
| 06 Background work | S16 |
| 07 Session log and trajectory | S18, all cases' evidence |
| 08 Model selection | S19 |
| 09 Tool catalogue | S01, S21 |
| 10 Direct native tool schemas | S01, S03, S13, compound run |
| 11 Native tool surface | S01–S06, S09–S11, S13, S16–S18 |
| 12 Hooks | S08 |
| 13 Sandbox | S09 |
| 14 Terminals and remote runtime | S10 |
| 15 Workspaces and files | S03, S09, S11, S18 |
| 16 In-app browser | S12 |
| 17 Extensions and plugins | S13, S20 |
| 18 MCP and connectors | S13 |
| 19 Composer and Start screen | S03, S12, S14, S18 |
| 20 Security and encryption | S09, S11, S14 |
| 21 Presets and agent modes | S03, S14 |
| 22 Configuration reference | S09, S14, S19 |
| 24 Operations and troubleshooting | Safe-profile setup, S19, failure triage |
| 25 Glossary | Terminology reference for the run ledger |
| 26 Browser Automation | S15 |
| 27 Computer-Use Provider | S07, S15 |
| 28 Session telemetry | S19 |
| 29 Runtime invariants | S03, S17 |
| 30 Plugin HMR and configuration service | S13, S20 |
| 31 Slash-command registry | S20 |
| 32 Permission presets and auto-review | S14, S20 |
| 33 Bundled skills | S20 |
| 34 Session format migration | S18 |
| 35 Conversation fork | S18 |
| 36 Session feedback | S18 |
| 37 Headless profile | S22 |
| 38 Workflow orchestration | S05, S17, compound run |
| 39 Goal-driven conversations | S17 |
| 40 Deliverables and changed files | S03, S11, S18, compound run |
| 41 Local SearXNG | S21 |

Chapter 23 is not present in the current manual index. No test is assigned to
an undocumented chapter.

## What to watch for

- This playbook exercises one local host and one provider/model at a time. It
  does not certify every provider, OS cage, browser, connector, or deployment.
- Tool selection is model-dependent. A missing or unsupported function-call
  capability is a blocked prerequisite; it is not evidence that the tool
  implementation ran.
- Native reads can run together, but writes are ordered. MCP calls are
  serialized by design. Workflow child turns may overlap even when their host
  tool calls do not.
- Local development tasks use eager in-process Celery by default. That does
  not prove a separate worker, broker, or scheduler in another deployment.
- In-app browsing, Playwright automation, and virtual-desktop computer use
  are separate runtimes with separate limits and security boundaries.
- Hooks run outside the tool cage. Playwright runs outside the agent sandbox.
  The computer-use provider is documented for development/testing and is not
  hardened for untrusted execution.
- The local SQLite operator has all permissions. It cannot prove tenant
  isolation; use the isolated authenticated multi-tenant test environment for
  that claim.
- Service-only or unwired features must be reported as such. A test guide
  must not make an absent UI look like a working feature.
- Do not clean, reset, or migrate any default or active database, cache,
  object store, workspace, or application profile as part of this playbook.
