# 67. Subagent External Backend Configuration

DeepSpace supports multiple subagent backends beyond the in-process default.
This chapter documents the configuration and setup for each external backend.

## Use case

- A deployment wants subagents to run on a separate ACP-compatible server.
- A team uses Claude Code or Codex CLI and wants subagents to leverage those.
- The DSH SDK backend enables custom runtime integration.
- A KineSis installation runs one-shot subagents through its configured SDK CLI.

## Why it matters

- External backends isolate subagent execution from the main DeepSpace process.
- Each backend has different capabilities, authentication, and resource requirements.
- Tool/skill filtering is only enforced by the in-process backend.

## How it works

### Backend selection (`delegation.py`)

The `subagent` and `subagent_fork` tools accept a `backend` parameter:

```json
{
  "backend": "in-process" | "acp" | "claude-code" | "codex" | "dsh-sdk" | "kinesis-sdk",
  "backend_config": { ... }
}
```

| Backend | Description | Tool/skill filtering |
|---|---|---|
| `in-process` | Child runs in same Python process | ✅ Full (tool_allow/deny, skill_allow/deny) |
| `acp` | Agent Client Protocol server | ❌ Not enforced |
| `claude-code` | Claude Code CLI subprocess | ❌ Not enforced |
| `codex` | Codex CLI subprocess | ❌ Not enforced |
| `dsh-sdk` | DeepSeek Harness SDK runtime | ❌ Not enforced |
| `kinesis-sdk` | KineSis SDK subprocess | ❌ Filter requests are rejected |

### ACP backend (`subagent_providers.py:ACPSubagentProvider`)

**Prerequisites:**
- ACP server running (e.g., `dsh-acp-server` on port 8080)
- Server exposes `/acp` WebSocket endpoint

**Configuration:**
```json
{
  "backend": "acp",
  "backend_config": {
    "url": "ws://localhost:8080/acp",
    "headers": { "Authorization": "Bearer <token>" },
    "connect_timeout": 10
  }
}
```

**Capabilities:**
- Streams tool calls/results over WebSocket
- Supports `run_in_background` via ACP job control
- No tool/skill filtering (ACP server must enforce its own)

### Claude Code backend (`subagent_providers.py:ClaudeCodeProvider`)

**Prerequisites:**
- `claude` CLI installed and authenticated (`claude auth login`)
- Available in PATH or `backend_config.claude_path`

**Configuration:**
```json
{
  "backend": "claude-code",
  "backend_config": {
    "claude_path": "/usr/local/bin/claude",
    "working_dir": "/workspace",
    "env": { "ANTHROPIC_API_KEY": "..." }
  }
}
```

**Capabilities:**
- Runs `claude --print --model <model> --prompt <task>`
- One-shot only (no `continuable` mode)
- No tool/skill filtering
- Output captured via stdout

### Codex backend (`subagent_providers.py:CodexProvider`)

**Prerequisites:**
- `codex` CLI installed and authenticated
- Available in PATH or `backend_config.codex_path`

**Configuration:**
```json
{
  "backend": "codex",
  "backend_config": {
    "codex_path": "/usr/local/bin/codex",
    "working_dir": "/workspace"
  }
}
```

**Capabilities:**
- Runs `codex exec --model <model> --prompt <task>`
- One-shot only
- No tool/skill filtering
- Output captured via stdout

### DSH SDK backend (`subagent_providers.py:DSHSDKProvider`)

**Prerequisites:**
- DeepSeek Harness SDK installed (`pip install deepseek-harness`)
- SDK configuration file (`~/.kinesis/config.yaml`)

**Configuration:**
```json
{
  "backend": "dsh-sdk",
  "backend_config": {
    "config_path": "/etc/dsh/config.yaml",
    "workspace": "default"
  }
}
```

**Capabilities:**
- Uses DSH's native agent runtime
- Supports all DSH tooling (MCP, workflows, etc.)
- One-shot only
- No tool/skill filtering

### KineSis SDK backend (`subagent_provider_kinesis_sdk.py`)

**Prerequisites:**
- The configured KineSis CLI is installed and available to the backend process.
- `KINESIS_HOME` is configured either in the provider entry or the process environment.

**Configuration:**
```json
{
  "deepspace_subagent_providers": {
    "kinesis-sdk": {
      "command": "kinesis",
      "kinesis_home": "/var/lib/kinesis",
      "profile": "sdk",
      "provider": "local-provider",
      "model": "local-model",
      "initialize_timeout_ms": 30000,
      "dispose_grace_ms": 3000
    }
  }
}
```

DeepSpace starts one CLI process per run, sends the prompt over stdin, and reads
the JSON response from stdout. Per-run environment values are passed to the
child, while the configured KineSis home is kept authoritative. The response
timeout terminates and reaps the child; cancellation does the same. Tool
allow/deny filters are rejected because this one-shot SDK interface cannot
enforce them.

### Tool filtering differences

| Feature | In-process | External backends |
|---|---|---|
| `tool_allow` / `tool_deny` | ✅ Enforced by `child_tool_names()` | ❌ Rejected at delegation |
| `skill_allow` / `skill_deny` | ✅ Enforced via `skill_scope` | ❌ Rejected at delegation |
| `agent_preset` | ✅ Narrows preset | ❌ Rejected (cannot enforce) |
| `continuable` mode | ✅ Full support | ❌ One-shot only |
| Background jobs | ✅ Via `ctx.jobs` | ❌ Not supported |

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_subagent_default_backend` | `str` | `"in-process"` | Default backend when not specified. |
| `deepspace_acp_server_url` | `str` | `""` | Default ACP server WebSocket URL. |
| `deepspace_claude_code_path` | `str` | `""` | Path to `claude` CLI. |
| `deepspace_codex_path` | `str` | `""` | Path to `codex` CLI. |
| `deepspace_dsh_sdk_config` | `str` | `""` | Path to DSH SDK config. |
| `deepspace_subagent_providers` | `dict[str, dict[str, Any]]` | `{}` | Per-provider CLI settings, including the `kinesis-sdk` entry above. |

### Example: Using ACP for heavy workloads

```json
{
  "name": "subagent",
  "arguments": {
    "description": "Large code audit",
    "task": "Audit all Python files for security issues",
    "backend": "acp",
    "backend_config": {
      "url": "wss://acp.example.com/acp",
      "headers": { "Authorization": "Bearer ${SECRETS.ACP_TOKEN}" }
    },
    "tool_allow": ["fs_read", "fs_glob", "fs_grep", "skill_load"],
    "skill_allow": ["badge-security"]
  }
}
```

### Example: Quick Codex one-shot

```json
{
  "name": "subagent",
  "arguments": {
    "description": "Write unit tests",
    "task": "Write pytest tests for utils/crypto.py",
    "backend": "codex",
    "model": "gpt-4o"
  }
}
```

## What to watch for

- **External backends cannot enforce tool/skill filters** — the delegation call
  will reject `tool_allow`/`tool_deny`/`skill_allow`/`skill_deny` for non-in-process backends.
- **Continuable mode not supported** — external backends only support `one-shot`.
- **Authentication** — ACP/Claude Code/Codex require prior auth; DSH SDK needs config.
- **Working directory** — external backends run in `backend_config.working_dir` or the
  conversation's workspace root; ensure the path exists on the target machine.
- **Output size** — external backend output is captured via stdout; large outputs
  may be truncated by the CLI's own limits.

## Related chapters

- [Delegation and Subagents](04-delegation-and-subagents.md) — core subagent mechanics.
- [Skill Scope and Delegation](55-skill-scope-and-delegation.md) — `skill_allow`/`skill_deny`.
- [The Tool Catalogue](09-the-tool-catalogue.md) — `subagent` tool reference.
