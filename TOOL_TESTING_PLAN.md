# DeepSpace Tool-Calling E2E Testing Infrastructure

## Objective
Build production-grade, 100% coverage tool-calling verification using LM Studio (spark-x2.6-1.7b) that:
- **Approach #2 (CI)**: Recorded fixtures replayed deterministically in CI
- **Approach #1 (Nightly)**: Real LM Studio model against disposable backends
- **Parallel execution**: 4 workers for recording, sequential for nightly
- **Zero misses**: Every tool, every category, every error path tested

---

## Tool Inventory (from `backend/app/deepspace/services/tools/__init__.py`)

### Core Tools (34 categories, 135+ tools)

| Category | Tools | Test Priority |
|----------|-------|---------------|
| **FS_TOOLS** | fs_read, fs_write, fs_edit, fs_delete, fs_list, fs_stat, fs_move, fs_patch | P0 - Core |
| **FS_SEARCH_TOOLS** | fs_search, fs_grep, fs_glob | P0 - Core |
| **WEB_TOOLS** | web_search, url_read, image_read | P0 - Core |
| **SHELL_TOOLS** | bash, pwsh, sandbox_execute | P0 - Core |
| **DELEGATION_TOOLS** | delegate_task, delegate_read, delegate_write | P1 - Advanced |
| **JOBS_TOOLS** | job_create, job_read, job_list, job_cancel | P1 - Advanced |
| **WORKFLOW_TOOLS** | workflow_create, workflow_step, workflow_status | P1 - Advanced |
| **RALPH_TOOLS** | ralph_query, ralph_search | P2 - Specialized |
| **GOAL_TOOLS** | goal_create, goal_update, goal_list | P1 - Planning |
| **SKILL_TOOLS** | skill_install, skill_list, skill_run | P1 - Extensibility |
| **OFFICE_TOOLS** | office_process, office_convert | P2 - Documents |
| **SPILL_TOOLS** | spill_create, spill_read, spill_write | P2 - Large data |
| **RUN_CODE_TOOLS** | python_exec, sql_exec, bash_exec (sandbox) | P0 - Code exec |
| **PLAN_TOOLS** | plan_create, plan_update, plan_read | P1 - Planning |
| **EXTENSION_TOOLS** | extension_install, extension_list | P2 - Extensibility |
| **TERMINAL_TOOLS** | terminal_open, terminal_read, terminal_write, terminal_close, terminal_list | P0 - Terminals |
| **DIFF_TOOLS** | diff_create, diff_apply | P1 - Code review |
| **SSH_TOOLS** | ssh_connect, ssh_exec, ssh_tunnel | P2 - Remote |
| **HOOK_TOOLS** | hook_register, hook_trigger | P2 - Events |
| **TYPE_GRAPH_TOOLS** | type_graph_query, type_graph_search | P2 - Code intel |
| **SCHEDULE_TOOLS** | schedule_create, schedule_list, schedule_cancel | P2 - Cron |
| **JOB_OUTPUT_TOOLS** | job_output_read, job_output_stream | P1 - Jobs |
| **READ_IMAGE_TOOLS** | image_read (duplicate?) | P0 - Media |
| **PRESENT_TOOLS** | present_create, present_update | P2 - Presentations |
| **MANAGE_TOOLS** | manage_config, manage_secrets | P2 - Admin |
| **INSPECT_TOOLS** | inspect_code, inspect_runtime | P2 - Debugging |
| **DEPENDENCY_TOOLS** | dep_graph, dep_check | P2 - Deps |
| **SESSION_QUERY_TOOLS** | session_query, session_list | P1 - Sessions |
| **TERMINAL_SIGNAL_TOOLS** | terminal_signal | P0 - Terminals |
| **TERMINAL_JOB_TOOLS** | terminal_job_create, terminal_job_read | P1 - Jobs |
| **MCP_RESOURCE_TOOLS** | mcp_resource_read, mcp_resource_list | P2 - MCP |
| **TEAM_TOOLS** | team_create, team_invite, team_list | P2 - Collaboration |
| **RUNTIME_DISCOVER_TOOLS** | runtime_discover | P2 - Discovery |
| **SSH_FS_TOOLS** | ssh_fs_read, ssh_fs_write, ssh_fs_list, ... (10 tools) | P2 - Remote FS |
| **SSH_TERMINAL_TOOLS** | ssh_terminal_open, ssh_terminal_read, ... (5 tools) | P2 - Remote term |
| **SSH_STREAM_TOOLS** | ssh_stream_read, ssh_stream_close | P2 - Remote streams |
| **INSTRUCTIONS_TOOLS** | instructions_read, instructions_write | P2 - Instructions |
| **LSP_TOOLS** | lsp_hover, lsp_definition, lsp_references, ... | P2 - LSP |
| **REFERENCE_TOOLS** | reference_create, reference_resolve | P2 - Refs |
| **COMPACT_TOOLS** | compact_conversation | P1 - Context |
| **BROWSER_AUTOMATION_TOOLS** | browser_navigate, browser_click, ... | P2 - Browser |
| **COMPUTER_USE_TOOLS** | computer_click, computer_type, ... | P2 - Computer use |

---

## Test Scenario Matrix

Each tool category needs scenarios covering:
| Scenario Type | Description | Example |
|---------------|-------------|---------|
| **Happy path** | Normal successful execution | `fs_read` existing file |
| **Error handling** | Tool returns error, model recovers | `fs_read` missing file → model handles gracefully |
| **Validation error** | Model sends invalid args → tool rejects | `fs_write` missing required field |
| **Chained calls** | Multiple tools in sequence | `web_search` → `url_read` → `fs_write` |
| **Parallel calls** | Independent tools called together | `fs_read` file A + `fs_read` file B |
| **Long-running** | Tool takes time, streaming results | `sandbox_execute` long script |
| **Interactive** | Tool asks user (ask_user) | `ask_user` → model waits → continues |
| **Permission gated** | Settings-gated tools | `bash` when shell disabled |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    TOOL TESTING INFRASTRUCTURE                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │  SCENARIO    │    │   RECORDER   │    │   FIXTURE    │      │
│  │  LIBRARY     │───▶│  (Approach 2)│───▶│   STORE      │      │
│  │  (YAML/JSON) │    │  LM Studio + │    │  (JSONL)     │      │
│  └──────────────┘    │  Real Backend│    └──────┬───────┘      │
│         │            └──────────────┘           │              │
│         │                   │                   │              │
│         ▼                   ▼                   ▼              │
│  ┌──────────────────────────────────────────────────────┐     │
│  │              REPLAY HARNESS (CI)                     │     │
│  │  • Loads fixtures                                    │     │
│  │  • Replays tool calls against real backend           │     │
│  │  • Asserts: no errors, correct results, complete     │     │
│  │  • Parallel: 4 workers                               │     │
│  └──────────────────────────────────────────────────────┘     │
│                           │                                     │
│                           ▼                                     │
│  ┌──────────────────────────────────────────────────────┐     │
│  │              NIGHTLY JOB (Approach 1)                │     │
│  │  • Real LM Studio model (spark-x2.6-1.7b)            │     │
│  │  • Disposable backend per scenario                   │     │
│  │  • Fresh scenarios + regression suite                │     │
│  │  • Detects drift, new tool failures                  │     │
│  │  • Sequential (stable)                               │     │
│  └──────────────────────────────────────────────────────┘     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Phases

### Phase 1: Foundation (Week 1)
- [ ] Tool inventory extraction script
- [ ] LM Studio client with tool-calling support
- [ ] Disposable backend fixture (pytest)
- [ ] Scenario library structure (YAML)

### Phase 2: Recorder (Week 1-2)
- [ ] `ToolCallRecorder` class - orchestrates LLM ↔ Backend loop
- [ ] Scenario executor - runs prompt through full tool-calling loop
- [ ] Fixture serializer - JSONL with full trace
- [ ] Parallel recorder (4 workers)
- [ ] Error capture & classification

### Phase 3: Replay Harness (Week 2)
- [ ] Fixture loader
- [ ] Deterministic replay engine
- [ ] Assertion suite (no errors, complete results, schema validity)
- [ ] CI integration (GitHub Actions / pytest)
- [ ] Parallel replay (4 workers)

### Phase 4: Nightly Job (Week 2-3)
- [ ] Nightly scheduler (cron / GitHub Actions schedule)
- [ ] Disposable backend per scenario
- [ ] Fresh scenario generation (LLM creates new prompts)
- [ ] Drift detection (compare nightly vs fixture baseline)
- [ ] Alerting on new failures

### Phase 5: Comprehensive Scenarios (Week 3-4)
- [ ] P0 core tools: 50+ scenarios
- [ ] P1 advanced tools: 30+ scenarios
- [ ] P2 specialized tools: 20+ scenarios
- [ ] Cross-tool chaining scenarios: 20+
- [ ] Error/edge cases: 30+

### Phase 6: Polish & CI (Week 4)
- [ ] Full CI pipeline
- [ ] Documentation
- [ ] Performance baselines
- [ ] Flakiness elimination

---

## Success Criteria

| Metric | Target |
|--------|--------|
| Tool coverage | 100% (all 135+ tools) |
| Scenario count | 150+ scenarios |
| CI replay time | < 5 minutes (parallel) |
| Nightly job time | < 30 minutes |
| False positive rate | 0% |
| Drift detection | 100% of breaking changes |
| Tool error detection | 100% (validation, runtime, timeout) |

---

## Files to Create

```
backend/tests/tool_calling/
├── __init__.py
├── conftest.py                      # Pytest fixtures: disposable backend, LM Studio client
├── lm_studio_client.py              # OpenAI-compatible client for LM Studio
├── disposable_backend.py            # Spawns isolated FastAPI + DB per test
├── scenario_library/
│   ├── __init__.py
│   ├── core_tools.yaml              # P0 scenarios
│   ├── advanced_tools.yaml          # P1 scenarios
│   ├── specialized_tools.yaml       # P2 scenarios
│   ├── chaining_scenarios.yaml      # Multi-tool flows
│   └── error_scenarios.yaml         # Error handling
├── recorder/
│   ├── __init__.py
│   ├── tool_call_recorder.py        # Main recording orchestration
│   ├── scenario_executor.py         # Executes one scenario end-to-end
│   ├── fixture_serializer.py        # JSONL output
│   └── parallel_recorder.py         # 4-worker parallel recording
├── replay/
│   ├── __init__.py
│   ├── fixture_loader.py            # Loads recorded fixtures
│   ├── replay_engine.py             # Deterministic replay
│   ├── assertions.py                # Comprehensive assertions
│   └── parallel_replay.py           # 4-worker parallel replay
├── nightly/
│   ├── __init__.py
│   ├── nightly_runner.py            # Nightly job entry point
│   ├── fresh_scenario_generator.py  # LLM generates new prompts
│   ├── drift_detector.py            # Compares nightly vs baseline
│   └── alerter.py                   # Failure notifications
├── fixtures/                        # Recorded fixtures (git-tracked)
│   ├── core_tools.jsonl
│   ├── advanced_tools.jsonl
│   └── ...
└── run_recorder.py                  # CLI: python -m backend.tests.tool_calling.run_recorder
```

---

## LM Studio Integration Details

```python
# Configuration
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
PRIMARY_MODEL = "spark-x2.6-1.7b"
MAX_PARALLEL_WORKERS = 4
TOOL_CALL_TIMEOUT = 30  # seconds
MAX_TOOL_CALLS_PER_TURN = 10
MAX_TURNS_PER_SCENARIO = 20
```

### Tool Calling Loop
```python
async def run_tool_calling_loop(prompt: str, tools: list[dict], backend_client) -> Trace:
    messages = [{"role": "user", "content": prompt}]
    trace = []

    for turn in range(MAX_TURNS_PER_SCENARIO):
        # 1. Call LM Studio with tools
        response = await lm_studio.chat.completions.create(
            model=PRIMARY_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            timeout=TOOL_CALL_TIMEOUT
        )

        # 2. Extract tool calls
        tool_calls = response.choices[0].message.tool_calls or []
        if not tool_calls:
            # Model responded without tools - scenario complete
            trace.append({"type": "final_response", "content": response.choices[0].message.content})
            break

        # 3. Execute each tool call against REAL backend
        for tc in tool_calls:
            result = await backend_client.execute_tool(tc.function.name, json.loads(tc.function.arguments))
            trace.append({"type": "tool_call", "call": tc, "result": result})
            messages.append({"role": "assistant", "tool_calls": [tc]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

    return trace
```

---

## Fixture Format (JSONL)

```jsonl
{"scenario_id": "fs_read_001", "category": "core", "prompt": "Read the file /tmp/test.txt", "tools_available": ["fs_read", "fs_write", ...], "trace": [{"type": "tool_call", "call": {"name": "fs_read", "arguments": {"path": "/tmp/test.txt"}}, "result": {"status": "success", "content": "hello world"}}, {"type": "final_response", "content": "The file contains: hello world"}], "assertions": {"no_errors": true, "all_tools_executed": true, "complete": true}}
```

---

## Next Steps

1. **Approve this plan** - I'll start with Phase 1 immediately
2. **Provide any custom scenarios** - Specific workflows you want covered
3. **Confirm LM Studio stays running** - During recording phase
4. **Review fixture format** - Any additional metadata needed?

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| LM Studio model quality varies | Record multiple runs, take consensus; nightly catches drift |
| Tool side effects (file writes, shell cmds) | Disposable backend + temp directories; rollback on failure |
| Flaky tests | Deterministic replay; nightly runs fresh; quarantine flaky scenarios |
| Recording takes too long | Parallel (4 workers); incremental recording; cache successful runs |
| Nightly job fails silently | Alerting + artifact upload; GitHub Actions summary |
