# 43. Manual end-to-end run ledger

Use this template with the [manual end-to-end test playbook](42-manual-e2e-test-playbook.md).
Make one copy per test campaign. Keep identifiers and raw logs private when
they could reveal prompts, local paths, account details, or tool arguments.

## Use case

Use this ledger to record repeated manual runs of DeepSpace and the evidence
that supports each pass, failure, or blocked prerequisite.

## Why it matters

A successful answer alone does not prove that the expected tool, child agent,
policy check, or runtime actually ran. Recording the configuration, trace,
timings, and fixture state makes runs comparable and makes failures
reproducible without exposing sensitive data.

## How it works

Copy the campaign and attempt sections for the playbook cases you run. Keep
the result label separate from the app's status, attach only redacted evidence,
and record exact prerequisites for cases that cannot run. Do not mark a
service-only feature or an unavailable capability as passed.

## Campaign record

| Field | Value |
|---|---|
| Campaign name / run ID | |
| Date and timezone | |
| Repository revision or app build | |
| Host OS / architecture | |
| Test profile path (redact before sharing) | |
| Backend and frontend ports | |
| Local profile or isolated multi-tenant setup | |
| Provider and model IDs (no credentials) | |
| Tool-call support confirmed | |
| Parallel-call support confirmed | |
| Image/computer-use capability, if tested | |
| Agent mode / sandbox mode / approval mode | |
| Selected workspace | |
| Sandbox backend / enforcement level | |
| Browser runtime and Playwright availability | |
| MCP/connector fixture identifiers | |
| SearXNG status | |
| Scheduler/worker mode | |
| Start time / end time | |
| Operator | |

### Safety and scope

- [ ] The test used a disposable profile/workspace, not the daily profile.
- [ ] Ports were confirmed unused; no running app/service was stopped to free one.
- [ ] Provider, MCP, connector, SSH, and browser targets are test-only or loopback.
- [ ] No real data, secrets, personal account, VPS, or production tenant was used.
- [ ] The selected sandbox reported the expected backend and `sandboxed` state.
- [ ] Temporary terminals, browser sessions, schedules, teammates, and connectors were closed/paused/removed.
- [ ] The test profile remains available until the evidence is reviewed.

## Case summary

Use the playbook's PASS, FAIL, BLOCKED, INCONCLUSIVE, and NOT APPLICABLE
labels. A service-only or unwired UI path should be written explicitly in the
notes.

| Case | Capability | Required repetitions | Attempt results | Summary status | Linked evidence |
|---|---|---:|---|---|---|
| S01 | Direct preset schemas, native tools, discovery-loop stop | 3 | | | |
| S02 | Multiple tool calls / parallel native reads | 5 | | | |
| S03 | Agentic loop, plan, final answer | 3 | | | |
| S04 | One-shot and continuable children | 3 | | | |
| S05 | Parallel child work / nested delegation | 5 | | | |
| S06 | Named teammates and shared board | 3 | | | |
| S07 | VNC/Xvfb computer use | 3 | | | |
| S08 | Hooks allow/block/ask/audit | 3 per decision | | | |
| S09 | Sandbox modes and boundary checks | 3 per boundary | | | |
| S10 | Local terminal / optional loopback SSH | 3 | | | |
| S11 | Workspace, files, Library, notes, memory | 3 | | | |
| S12 | In-app browser panel | 3 | | | |
| S13 | Extensions, plugins, MCP, connectors | 3 per enabled surface | | | |
| S14 | Auth boundary, modes, permissions, approvals | 3 | | | |
| S15 | Playwright and computer-use distinction | 3 | | | |
| S16 | Jobs, queue, schedules | 3 | | | |
| S17 | Workflows, goals, loop guard | 3 | | | |
| S18 | Trajectory, export, fork, feedback, deliverables | 3 | | | |
| S19 | Model selection, telemetry, measurements | 3 | | | |
| S20 | Service-only registry surfaces | 3 per service | | | |
| S21 | Web search, URL reading, citations | 3 | | | |
| S22 | Headless CLI / sdk-minimal | 3 | | | |
| Compound | Bounded multi-capability task | 3 | | | |

## Attempt record

Copy this section for each attempt.

### Case ID / attempt

| Field | Value |
|---|---|
| Case and attempt number | |
| Conversation ID (private) | |
| Fresh conversation? | |
| Provider/model | |
| Agent mode / sandbox / workspace | |
| Prerequisites checked | |
| Start time / end time / duration | |
| Final status (`ready`, `blocked`, `failed`, etc.) | |
| Result label | |

**Prompt or UI action:**

```text

```

**Expected behavior:**

```text

```

**Observed result:**

```text

```

**Trajectory evidence:**

| Evidence | Value |
|---|---|
| `turn/start` / `turn/end` sequence or trace link | |
| Provider model rounds / steps | |
| Tool-call names and count | |
| Completed / failed / refused tool results | |
| First-request authorized schemas / no `discover_tools` schema | |
| Child IDs / teammate names / workflow run IDs | |
| Hook/invariant/approval events | |
| Final assistant result | |

**Timing and reliability measurements:**

| Metric | Value |
|---|---:|
| Time to first visible assistant text (ms) | |
| End-to-end turn duration (ms) | |
| Provider latency (ms, if present) | |
| Tool durations (ms) | |
| Same-step independent read calls | |
| Overlapping read intervals proven? | |
| Child-turn intervals / max simultaneous children | |
| Input/output tokens (if returned) | |
| Retry, timeout, refusal, or duplicate count | |

**Data and security checks:**

- Expected fixture output/hash:
- Actual fixture output/hash:
- Outside-workspace sentinel before/after:
- `sandboxed`, backend, enforcement, `failure_kind`:
- Cross-conversation/tenant scope result:
- Secret or personal data observed (expected: none):
- Temporary resources left running:

**Evidence files / screenshots (test profile only):**

```text

```

**Notes, errors, and follow-up:**

```text

```

## Repeatability and aggregate measurements

| Metric | Attempt values | p50 | p95 | Notes / conditions |
|---|---|---:|---:|---|
| End-to-end turn duration | | | | |
| Time to first visible text | | | | |
| Provider latency | | | | |
| Native tool duration | | | | |
| Parallel read batch wall time | | | | |
| Child-turn completion time | | | | |
| Successful expected tool results | | | | |
| Unexpected failed tool results | | | | |
| Missing/duplicate events or mutations | | | | |

Do not compare timings across different models, providers, prompt sizes,
fixture states, or hosts as if they were a controlled benchmark. Report the
exact conditions beside every aggregate. Three runs are a repeatability
smoke check, not a statistically stable latency estimate.

## Defects and disposition

| ID | Case / attempt | Expected | Observed | Evidence | Severity | Reproducible? | Status / owner |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

## Campaign completion checklist

- [ ] Each enabled case has the required number of attempts or a documented reason it is blocked.
- [ ] No blocked or service-only case is counted as a pass.
- [ ] Parallelism claims have same-step and overlap evidence, not just a fast-looking answer.
- [ ] All fixture totals, file hashes, deliverables, and event order were checked.
- [ ] Security boundary failures stopped the campaign and were recorded as failures.
- [ ] Schedules, terminals, browser sessions, connectors, hook manifests, and test workspace changes were cleaned up or intentionally retained.
- [ ] Active application data, services, credentials, and workspaces were left untouched.
- [ ] The campaign report lists the provider/model, runtime configuration, exact unavailable prerequisites, and checks not run.

## What to watch for

- Keep conversation IDs, tenant/user identifiers, local paths, prompts, and
  event payloads private unless they have been safely redacted.
- Use the playbook's safety rules for disposable profiles and services; this
  ledger does not make an unsafe target safe.
- A small sample can confirm repeatability but does not establish a stable
  latency percentile or certify all providers and operating systems.
