# 37. Headless Local Run Profile

DeepSpace provides a direct headless CLI and a short-lived `sdk-minimal` profile for running one agent turn without a browser. The direct CLI uses the same database and chat service as the server, while `sdk-minimal` is a newline-delimited JSON-RPC adapter around the same in-process runner.

## Use case

- Developers want to run DeepSpace locally for testing/debugging
- CI/CD pipelines need to run agent tasks without server infrastructure
- Users want a simple `python -m app.deepspace.cli headless "prompt"` CLI experience

## Why it matters

- Previously, DeepSpace required a running server (HTTP + WebSocket)
- Now the `python -m app.deepspace.cli headless` entry point runs the agent in-process with the configured database
- Same agent logic, tools, and session persistence as server mode

## How it works

### Architecture

1. **Configuration**: `HeadlessConfig` and CLI flags select a run. When a
   flag is omitted, the CLI copies the corresponding `deepspace_headless_*`
   setting into `HeadlessConfig`.
2. **Runner**: `HeadlessRunner` wraps `DeepSpaceChatService` in-process
3. **CLI**: `python -m app.deepspace.cli headless` with prompts, options, and output formats
4. **Database**: Uses the same SQLite/PostgreSQL session factory as the server; a new runner creates a new conversation unless a conversation is supplied through the runner API or interactive CLI mode

The direct profile prints the turn result for a human or script. The
`sdk-minimal` profile accepts newline-delimited JSON-RPC, handles
`initialize`, runs one `turn`, emits `turn/update` and `tool/call`
notifications, returns the final result, and exits. It passes tenant and user
IDs to the runner when the caller supplies them. Every direct turn is bounded
by `deepspace_headless_turn_timeout_seconds`.

### Declared settings

These fields exist in `Settings` for deployment configuration. Until the CLI
binds them to `HeadlessConfig`, use the CLI flags or construct `HeadlessConfig`
directly when their values must affect a run.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_headless_enabled` | bool | `true` | Rejects headless startup when false |
| `deepspace_headless_default_tenant` | str | `null` | Default tenant when `--tenant` is omitted |
| `deepspace_headless_default_user` | str | `null` | Default user when `--user` is omitted |
| `deepspace_headless_default_model` | str | `null` | Default model when `--model` is omitted |
| `deepspace_headless_max_turns` | int | `100` | Maximum prompts processed in one multi-prompt conversation |
| `deepspace_headless_turn_timeout_seconds` | int | `300` | Per-turn wall-clock timeout |
| `deepspace_headless_output_format` | enum | `"json"` | Output format when `--format` is omitted |
| `deepspace_headless_show_reasoning` | bool | `false` | Adds redacted provider reasoning to direct JSON/text output when available |
| `deepspace_headless_show_tools` | bool | `true` | Adds collected tool calls to direct JSON output and a count to text output |

### CLI usage

```bash
# Run these commands from AverQel Harness/backend.
# Single prompt
python -m app.deepspace.cli headless "Write a Python function to compute fibonacci"

# Multiple prompts (conversation)
python -m app.deepspace.cli headless "Create a plan" "Execute step 1" "Execute step 2"

# Continue an existing conversation in interactive mode
python -m app.deepspace.cli headless --interactive --conversation <conversation-id>

# Interactive mode
python -m app.deepspace.cli headless --interactive

# JSON output for scripting (turn, success, content, and error)
python -m app.deepspace.cli headless --format json "List files in workspace"

# One-turn newline JSON-RPC child profile
python -m app.deepspace.cli headless --profile sdk-minimal --tenant <tenant-id> --user <user-id>
```

### Output formats

**JSON** (default):
```json
{
  "turn": 1,
  "success": true,
  "content": "Here's the fibonacci function...",
  "error": null
}
```

**JSONL** (one result object per prompt):
```json
{"turn": 1, "success": true, "content": "Here's the...", "error": null}
```

**Text** (human-readable):
```
Turn 1: Here's the fibonacci function...
```

### Programmatic usage

```python
from app.core.config import get_settings
from app.deepspace.cli.headless import HeadlessRunner, HeadlessConfig

config = HeadlessConfig(
    default_tenant="...",
    default_user="...",
    max_turns=50,
    output_format="json",
)

runner = HeadlessRunner(config, get_settings())
await runner.initialize()

results = await runner.run_conversation([
    "Create a Python project",
    "Add tests",
    "Run tests",
])

for result in results:
    print(result.content)
```

### Programmatic turn

```python
result = await runner.run_turn(
    prompt="Refactor the auth module",
    conversation_id=existing_conv_id,
    model="gpt-4",  # Must be a model route available to this tenant/user.
)
```

### Database

- Uses same session factory as server (`get_session_factory()`)
- Tenant/user context set per runner
- Conversations persisted to same tables as server mode
- No server process needed; runs entirely in-process

## What to watch for

- **Database**: Uses same DB as server; ensure migrations applied
- **Secrets**: Needs provider API keys in environment/credential store
- **Concurrency**: A direct runner owns one database session; use separate runner processes for concurrent runs
- **Resources**: Runs full agent loop in-process; memory/CPU similar to worker
- **Output**: JSON/JSONL for scripting; text for human use. Direct output
  follows the tool and reasoning visibility settings; the `sdk-minimal`
  protocol always emits `tool/call` notifications as part of its wire format.
- **Interactive**: Reads from stdin; Ctrl+C to exit
- **Tenants**: Default tenant/user from settings or CLI args
- **Model**: Default model from settings or `--model` override
- **Timeout**: A timeout returns a failed turn with the partial content and
  collected tool calls; the underlying service may still have durable
  cancellation/checkpoint state to resume separately.
