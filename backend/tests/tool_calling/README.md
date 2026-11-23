# DeepSpace Tool-Calling E2E Testing Infrastructure

Production-grade, 100% coverage tool-calling verification for DeepSpace's 133+ native tools.

## Overview

This infrastructure provides two complementary approaches:

| Approach | Use Case | Speed | Deterministic | Requirements |
|----------|----------|-------|---------------|--------------|
| **#2: Fixture Replay (CI)** | Every PR, fast feedback | ~2 min | ✅ Yes | Pre-recorded fixtures |
| **#1: Nightly Real Model** | Drift detection, regression | ~30 min | ❌ No (real LLM) | LM Studio running |

## Quick Start

### 1. Prerequisites

```bash
# LM Studio running with model loaded
# Default: http://localhost:1234/v1 with spark-x2.6-1.7b

# Backend dependencies
cd backend
python -m pip install -r requirements.txt -r requirements-dev.txt
```

### 2. Record Fixtures (Approach #2) - Run Once Locally

```bash
# Record core tools (P0) - ~5 minutes with 4 workers
cd backend
python -m tests.tool_calling.run_recorder --category core --workers 4

# Record all categories
python -m tests.tool_calling.run_recorder --category all --workers 4

# Record specific priority
python -m tests.tool_calling.run_recorder --category core --priority 0 --workers 4
```

**What this does:**
- Spawns 4 parallel workers, each with isolated backend + LM Studio connection
- Runs every scenario in the category against real LM Studio + real backend
- Records full traces (prompt → tool calls → results → final response) as JSONL fixtures
- Saves to `backend/tests/tool_calling/fixtures/<category>_tools.jsonl`

### 3. Replay Fixtures in CI (Approach #2) - Fast & Deterministic

```bash
# Replay all fixtures (CI command)
cd backend
python -m tests.tool_calling.run_replay --category all --workers 4

# Replay specific category
python -m tests.tool_calling.run_replay --category core --workers 4

# Validate fixtures
python -m tests.tool_calling.run_replay --validate

# Show coverage summary
python -m tests.tool_calling.run_replay --summary
```

**What this does:**
- Loads recorded fixtures
- Replays each tool call against a fresh disposable backend
- Verifies: no errors, correct results, assertions pass, consistency with recording
- Runs in parallel (4 workers) - completes in ~2 minutes
- **No LM Studio required** - pure backend verification

### 4. Nightly Verification (Approach #1) - Real Model Drift Detection

```bash
# Requires LM Studio running
cd backend
python -m tests.tool_calling.run_nightly \
  --model spark-x2.6-1.7b \
  --fresh-count 20 \
  --alert
```

**What this does:**
- Replays all baseline fixtures (verifies they still work)
- Generates 20 fresh scenarios using the LLM itself
- Executes fresh scenarios against real LM Studio + backend
- Compares results with baseline to detect drift
- Sends alerts (email/Slack/webhook) on failure or drift

## Architecture

```
backend/tests/tool_calling/
├── conftest.py                    # Pytest config, fixtures
├── extract_tools.py               # Tool inventory extraction
├── tool_inventory.json            # 133 tools metadata
├── lm_studio_client.py            # LM Studio async client + orchestrator
├── disposable_backend.py          # Isolated backend per test
├── scenario_library/              # 150+ test scenarios (YAML)
│   ├── __init__.py
│   ├── core_tools.yaml            # P0: fs, web, shell, terminal, todo, artifact
│   ├── advanced_tools.yaml        # P1: plan, job, workflow, delegation, skill
│   ├── chaining_scenarios.yaml    # Multi-tool workflows
│   ├── error_scenarios.yaml       # Validation, runtime, recovery errors
│   └── specialized_tools.yaml     # P2: SSH, browser, computer, etc.
├── recorder/                      # Approach #2: Fixture Recording
│   ├── tool_call_recorder.py      # Main recording orchestration
│   ├── scenario_executor.py       # Single scenario execution
│   ├── fixture_serializer.py      # JSONL serialization
│   └── parallel_recorder.py       # 4-worker parallel recording
├── replay/                        # Approach #2: Fixture Replay (CI)
│   ├── fixture_loader.py          # Load recorded fixtures
│   ├── replay_engine.py           # Deterministic replay engine
│   ├── assertions.py              # Comprehensive assertions
│   └── parallel_replay.py         # 4-worker parallel replay
├── nightly/                       # Approach #1: Nightly Real Model
│   ├── nightly_runner.py          # Nightly run orchestration
│   ├── fresh_scenario_generator.py # LLM-generated fresh scenarios
│   ├── drift_detector.py          # Baseline vs fresh comparison
│   └── alerter.py                 # Email/Slack/webhook alerts
├── fixtures/                      # Recorded fixtures (git-tracked)
│   ├── core_tools.jsonl
│   ├── advanced_tools.jsonl
│   └── ...
├── nightly_reports/               # Nightly run reports
├── run_recorder.py                # CLI: python -m tests.tool_calling.run_recorder
├── run_replay.py                  # CLI: python -m tests.tool_calling.run_replay
├── run_nightly.py                 # CLI: python -m tests.tool_calling.run_nightly
└── test_recorder.py               # Pytest integration tests
```

## Scenario Library

### Core Tools (P0) - 30+ scenarios
- `fs_read`, `fs_write`, `fs_edit`, `fs_delete`, `fs_list`, `fs_glob`, `fs_grep`, `fs_str_replace`
- `web_search`, `url_read`
- `bash`, `pwsh`, `sandbox_execute` (python, sql, bash)
- `terminal_open`, `terminal_read`, `terminal_write`, `terminal_close`, `terminal_list`
- `todo_write`, `artifact_create`, `compact`

### Advanced Tools (P1) - 25+ scenarios
- `plan_submit`, `plan_read`, `goal_set`, `goal_read`
- `job_create`, `job_list`, `job_status`, `job_output`
- `workflow_run`, `delegate_task`, `skill_list`, `ralph_run`
- `session_search`, `spill_session`, `schedule_create`
- `type_graph`, `lsp`, `file_reference`, `run_code`, `present`

### Chaining Workflows (P0) - 15+ scenarios
- Research: search → read → write → artifact
- Code dev: write → test → write tests → run tests
- File pipelines: glob → read → grep → write → artifact
- Terminal + filesystem: mkdir → write → list
- Parallel calls: 3x fs_read, 3x fs_grep simultaneously

### Error Handling (P0) - 15+ scenarios
- Validation errors (missing required args)
- Runtime errors (missing file, failed command, timeout)
- Settings-gated unavailable tools
- Recovery patterns: retry, fallback, partial failure

### Specialized (P2) - 20+ scenarios
- SSH tools (require SSH server)
- Browser automation (require Playwright)
- Computer use (require display)
- Terminal jobs, signals, diff, analyze, dependencies

## Fixture Format (JSONL)

Each line is a complete trace:

```json
{
  "scenario_id": "fs_read_001",
  "category": "core",
  "prompt": "Read /workspace/README.md",
  "tools_available": ["fs_read", "fs_write", ...],
  "turns": [
    {"turn": 0, "role": "assistant", "tool_calls": [...], "finish_reason": "tool_calls"}
  ],
  "tool_calls": [
    {"turn": 0, "tool_call_id": "call_123", "tool_name": "fs_read", "arguments": {"path": "/workspace/README.md"}}
  ],
  "tool_results": [
    {"turn": 0, "tool_call_id": "call_123", "tool_name": "fs_read", "result": {"status": "success", "content": "# README\n..."}, "duration_ms": 45, "status": "success"}
  ],
  "final_response": "The file contains...",
  "errors": [],
  "started_at": 1234567890.123,
  "completed_at": 1234567895.456,
  "metadata": {
    "scenario_category": "core",
    "scenario_priority": 0,
    "expected_tools": ["fs_read"],
    "assertions": {"no_errors": true, "contains_in_response": ["README"]},
    "model": "spark-x2.6-1.7b"
  }
}
```

## CI Integration

### GitHub Actions (`.github/workflows/tool-calling-ci.yml`)

Runs on every push/PR:
1. **Validate fixtures** - integrity check
2. **Replay fixtures** - 4 parallel jobs (core, advanced, chaining, error)
3. **Tool coverage check** - 100% of 133 tools covered

### Nightly GitHub Actions (`.github/workflows/tool-calling-nightly.yml`)

Runs nightly at 2 AM UTC (requires self-hosted runner with LM Studio):
1. Baseline replay
2. Fresh scenario generation + execution
3. Drift detection
4. Alerts on failure/drift

## Commands Reference

### Recording
```bash
# Record core (P0) - recommended first run
python -m tests.tool_calling.run_recorder -c core -w 4

# Record all categories
python -m tests.tool_calling.run_recorder -c all -w 4

# Record only P0 across all categories
python -m tests.tool_calling.run_recorder -c all -p 0 -w 4

# List available scenarios
python -m tests.tool_calling.run_recorder --list-scenarios
```

### Replay (CI)
```bash
# Replay all
python -m tests.tool_calling.run_replay -c all -w 4

# Replay specific category
python -m tests.tool_calling.run_replay -c core -w 4

# Validate fixtures
python -m tests.tool_calling.run_replay --validate

# Show coverage summary
python -m tests.tool_calling.run_replay --summary
```

### Nightly
```bash
# Full nightly run
python -m tests.tool_calling.run_nightly --fresh-count 20 --alert

# With custom model
python -m tests.tool_calling.run_nightly --model lfm2.5-8b-a1b@q4_k_m --fresh-count 30

# Skip fresh scenarios (baseline only)
python -m tests.tool_calling.run_nightly --no-fresh
```

## Extending Scenarios

Add new scenarios to YAML files in `scenario_library/`:

```yaml
- id: "my_new_scenario"
  category: "core"        # core, advanced, chaining, error, specialized
  priority: 0             # 0=P0, 1=P1, 2=P2
  prompt: "Natural user request that triggers tool calls"
  expected_tools: ["fs_read", "fs_write"]
  min_tool_calls: 2
  max_tool_calls: 5
  max_turns: 6
  setup:
    create_files:
      /workspace/input.txt: "test content"
  assertions:
    no_errors: true
    file_created: "/workspace/output.txt"
  tags: ["custom", "feature"]
  description: "What this scenario tests"
```

## Troubleshooting

### LM Studio Connection Failed
```bash
# Check LM Studio is running
curl http://localhost:1234/v1/models

# Check model is loaded
curl -X POST http://localhost:1234/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "spark-x2.6-1.7b", "messages": [{"role": "user", "content": "hi"}]}'
```

### Fixture Recording Hangs
- Increase timeout: `LM_STUDIO_TIMEOUT=120`
- Reduce workers: `-w 2`
- Check LM Studio logs for model loading issues

### Replay Failures
- Run with `--validate` to check fixture integrity
- Check backend dependencies (Redis, etc.) are available
- Compare tool results with original fixture mismatches

### Tool Coverage Gaps
Run `python -m tests.tool_calling.run_replay --summary` to see which tools lack coverage, then add scenarios for missing tools.

## Performance Baselines

| Operation | Target |
|-----------|--------|
| Core fixture recording (30 scenarios, 4 workers) | < 5 min |
| Full fixture recording (150 scenarios, 4 workers) | < 20 min |
| Core fixture replay (4 workers) | < 1 min |
| Full fixture replay (4 workers) | < 3 min |
| Nightly run (baseline + 20 fresh) | < 30 min |

## Contributing

1. Add scenarios to appropriate YAML file
2. Run recorder locally to generate fixtures
3. Commit fixtures to git
4. CI will replay on every PR
5. Nightly catches drift automatically

## License

Part of AverQel Kinesis - Internal Testing Infrastructure
