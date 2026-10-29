# 32. Permission Presets + Auto-Review

DeepSpace provides a backend service with named permission presets (bundles of sandbox + approval settings) and optional LLM auto-review for tool calls. These permission presets are distinct from the web client's agent-mode presets. The current web Settings page edits agent-mode settings, not the `PermissionPresetService` presets.

## Use case

- Users want one-click security profiles instead of toggling individual settings
- Deployments want to enforce consistent security posture across conversations
- Teams want auto-review for known-safe tools to reduce approval fatigue

## Why it matters

- Previously, sandbox mode, approval mode, and network were separate toggles
- Now presets bundle related settings with semantic names
- Auto-review uses an LLM to approve known-safe tool calls automatically

## How it works

### Architecture

1. **Configuration**: `deepspace_permission_presets_enabled=true` plus auto-review settings
2. **Presets**: `PermissionPreset` bundles sandbox_mode, approval_mode, network, tools allow/deny lists, and runtime budgets
3. **Auto-review**: `AutoReviewEngine` uses LLM to review tool calls when enabled
4. **Service**: `PermissionPresetService` manages presets and auto-review

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_permission_presets_enabled` | bool | `true` | Deployment feature flag reserved for permission-preset integration |
| `deepspace_auto_review_enabled` | bool | `false` | Enable LLM auto-review |
| `deepspace_auto_review_provider` | str | `null` | Provider for auto-review |
| `deepspace_auto_review_model` | str | `null` | Model for auto-review |
| `deepspace_auto_review_prompt` | str | (built-in) | Review prompt template |

### Built-in presets

These six definitions belong to `PermissionPresetService`; they are not the
agent-mode presets published by `/api/v1/deepspace/chats/presets`, and selecting an
agent-mode preset does not automatically select one of these permission
presets. The `auto_review` column is preset metadata; the current chat path
also requires the global auto-review setting below to be enabled.

| Preset | Sandbox | Approval | Network | Auto-review | Tools |
|--------|---------|----------|---------|-------------|-------|
| `strict` | read-only | ask | off | no | - |
| `standard` | workspace-write | ask | on | no | - |
| `permissive` | workspace-write | auto | on | yes | - |
| `danger` | danger-full-access | auto | on | yes | - |
| `coding` | workspace-write | ask | on | no | fs_*, terminal, bash, lsp |
| `research` | read-only | ask | on | no | web_*, fs_read, session_* |

### Auto-review

When the global `deepspace_auto_review_enabled` setting is true and a tool call requires approval:

1. Tool call intercepted before execution
2. `AutoReviewEngine.review(tool_name, arguments, context)` called
3. LLM prompted with tool details + safety guidelines
4. LLM responds `approve`/`deny` + reason
5. If approve: tool executes immediately
6. If deny, times out, or cannot obtain a valid decision: the tool call is denied; it does not silently bypass policy or fall back to approval

```python
service = get_permission_preset_service(settings)

# Get effective settings for a preset
settings = service.get_effective_settings("coding")
# {"sandbox_mode": "workspace-write", "approval_mode": "ask", ...}

# Auto-review a tool call
approved, reason = await service.auto_review_tool(
    "fs_read", {"path": "src/main.py"}, {"conversation_id": "..."}
)
```

### Custom presets

```python
from app.deepspace.services.permission_presets import PermissionPreset

custom = PermissionPreset(
    id="my_preset",
    name="My Preset",
    description="Custom security profile",
    sandbox_mode="workspace-write",
    approval_mode="ask",
    network_allowed=True,
    tools_allowlist=("fs_read", "fs_write", "terminal"),
)
service.add_custom_preset(custom)
```

### Current web integration

The Settings tab includes an **Agent presets** selector and editor:

- Dropdown with the agent-mode presets published by `/api/v1/deepspace/chats/presets`
- Editors for sandbox mode, reasoning effort, delegation depth, tool allowlists, spill threshold, PTC, and plan-mode defaults
- A saved default for new conversations

It does not currently expose the `PermissionPresetService` built-in permission presets or an auto-review toggle. Permission preset and auto-review behavior is configured through backend settings/service integration.

## What to watch for

- **Auto-review costs**: Each review calls an LLM; monitor token usage and latency
- **Prompt injection**: Auto-review prompt includes tool arguments; sanitize if user-controlled
- **Fallback**: Auto-review denial, timeout, or reviewer failure denies the tool call; it never silently approves or falls back to an unreviewed execution
- **Preset conflicts**: Custom preset IDs must not collide with built-ins
- **Audit trail**: The approval stream reports `tool_auto_approved` with the reviewer source and reason; verify any separate durable session-event projection before treating it as a session-log field
- **Latency**: Auto-review adds one LLM round-trip per tool call; consider caching
- **Safety**: Auto-review is off by default and requires `deepspace_auto_review_enabled=true`; preset metadata alone does not enable it
