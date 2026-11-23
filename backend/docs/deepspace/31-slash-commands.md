# 31. Slash-Command Registry

DeepSpace has a centralized slash-command registry and a composer picker for
`/help`, `/compact`, `/goal`, `/permission`, `/feedback`, `/plan`, `/persona`,
`/skills`, `/export`, and registered custom commands. The picker suggests
commands; execution is split between the composer and authenticated host API.

## Use case

- Users need a discoverable way to run conversation commands without sending them as model prompts.
- Developers need one registry for command parsing, validation, help, and completion.

## Why it matters

- The composer loads registered command descriptions from `GET /api/v1/deepspace/host/slash-commands` and merges them with its built-in suggestions.
- Typing `/` filters suggestions; arrow keys, Enter, Tab, and Escape control the picker.
- The authenticated execution route checks conversation ownership before invoking a handler.

## How it works

### Architecture

1. **Configuration**: `deepspace_slash_commands_enabled=true` controls registry construction.
2. **Registry**: `SlashCommandRegistry` holds commands and handles execution.
3. **Parser**: `SlashCommandParser` uses `shlex` for POSIX-style argument parsing.
4. **Commands**: `SlashCommand` (abstract) or `FunctionSlashCommand` (function-backed).
5. **Execution**: `POST /api/v1/deepspace/host/chats/{conversation_id}/slash-command` builds its context from the authenticated principal and calls `registry.execute(input, context)` after verifying ownership.

### Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `deepspace_slash_commands_enabled` | bool | `true` | Master switch |
| `deepspace_slash_max_args` | int | `20` | Max positional arguments |
| `deepspace_slash_max_arg_length` | int | `1024` | Max chars per argument |
| `deepspace_slash_completion_limit` | int | `50` | Max completions |
| `deepspace_slash_help_width` | int | `80` | Help text wrap width |

### Built-in commands

| Command | Service result |
|---------|---------------|
| `/help` | Lists registered commands or returns one command's help. |
| `/compact` | Returns a compaction action payload; it does not invoke the compaction service. |
| `/goal` | Delegates goal operations to the conversation-scoped goal service. |
| `/permission` | Returns a requested permission mode; it does not persist the mode. |
| `/feedback` | Returns feedback data; it does not write the feedback API record. |
| `/plan` | Returns plan-mode data; it does not persist plan mode. |
| `/persona` | Returns the fixed `not configured` response. |
| `/skills` | Returns an empty skills response; it does not call the workspace skill tools. |
| `/export` | Validates format and returns an export-started message; it does not stream the export. |

### Custom commands

```python
from app.deepspace.services.slash_commands import (
    get_slash_command_registry, FunctionSlashCommand, SlashCommandContext, SlashCommandResult
)

async def my_command(ctx: SlashCommandContext) -> SlashCommandResult:
    name = ctx.args[0].value if ctx.args else "world"
    return SlashCommandResult(success=True, message=f"Hello, {name}!")

registry = get_slash_command_registry(settings)
registry.register(FunctionSlashCommand(
    name="hello",
    func=my_command,
    description="Say hello",
    usage="[name]",
    completer=lambda partial: ["world", "there"] if "world".startswith(partial) else [],
))
```

### Composer behavior and limits

Typing `/` in the composer opens the suggestion picker. Selecting `/plan`
toggles plan mode in the composer. Selecting `/compact` or `/help` calls the
execution route; `/export` uses the session ZIP endpoint and downloads the
archive. Other commands are inserted into the composer so the user can review
or edit their arguments before submitting. Submitting a recognized slash
command calls the execution route instead of sending it as a model prompt.

The registry returns a command result; it does not persist every action
implied by a command. `/permission`, `/feedback`, `/plan`, `/persona`,
`/skills`, and `/export` return descriptive data or status messages. The
composer owns plan-mode changes, and the separate export endpoint owns ZIP
downloads. `/compact` currently returns an action result without invoking the
context compaction service.

### Parser details

- Uses `shlex.split()` for POSIX-style quoting: `/cmd "arg with spaces" key=value`
- Arguments: positional (`arg1 arg2`) and keyword (`key=value`)
- Quoted strings preserve whitespace
- Escaped quotes supported (`\"`)

## What to watch for

- **Parsing**: Uses `shlex` (POSIX); Windows-style paths with backslashes need quoting
- **Validation**: The parser limits positional argument count and length; keyword arguments are not subject to those two limits, and commands should validate their own values
- **Context**: The API derives `tenant_id` and `user_id` from authentication and checks conversation ownership before constructing command context.
- **Async**: Command handlers may be synchronous or asynchronous; asynchronous results are awaited by the registry.
- **Continuation**: `continue_conversation` is returned to the composer, which decides whether to submit a normal chat turn.
- **Built-in handlers**: `/goal` delegates supported operations, including create, list, update, and complete, to the goal command service. `/compact --llm` is parsed as a positional token by the current parser, so the handler does not enable its LLM flag from that spelling.
- **Hidden commands**: Set `hidden=True` to exclude from `/help` and completions
- **Conversation required**: Set `requires_conversation=False` for global commands
- **Completions**: `get_completions()` returns bounded command-name matches when an embedding caller requests them; no current composer calls it. `help_width` is stored in configuration but is not currently applied to generated help text.
