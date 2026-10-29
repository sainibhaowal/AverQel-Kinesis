# 31. Slash-Command Registry

DeepSpace contains a centralized slash-command registry for `/help`,
`/compact`, `/goal`, `/permission`, `/feedback`, `/plan`, `/persona`,
`/skills`, `/export`, and custom commands. In the current release this is a
backend service API, not a composer or chat-route integration: callers must
invoke the registry explicitly.

## Use case

- A caller needs a discoverable, consistent way to invoke meta-commands
- Developers need a framework to add custom slash commands with argument parsing, completion, and help
- The registry handles parsing, validation, execution, and tab-completion

## Why it matters

- Previously, slash commands were hardcoded in the composer with no extension point
- The service registers commands centrally with argument parsing, validation, and completion helpers
- Custom commands can be added by plugins or deployments
- A future UI can use `/help`, completion results, and argument hints; the current web composer does not call this registry

## How it works

### Architecture

1. **Configuration**: `deepspace_slash_commands_enabled=true` is declared for registry construction; the current registry does not itself enforce the flag during `execute()`
2. **Registry**: `SlashCommandRegistry` holds all commands, handles parsing/execution
3. **Parser**: `SlashCommandParser` uses `shlex` for POSIX-style argument parsing
4. **Commands**: `SlashCommand` (abstract) or `FunctionSlashCommand` (function-backed)
5. **Integration**: An application caller must pass input and context to `registry.execute(input, context)`; no current composer/API adapter does this

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
| `/goal` | Returns goal action data; it does not persist a goal. |
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

### Current integration status

The current web composer and chat service do not call this registry. The
separate session-export HTTP endpoint remains the implemented export path.
The registry can be used by an embedding caller or a future adapter.

### Parser details

- Uses `shlex.split()` for POSIX-style quoting: `/cmd "arg with spaces" key=value`
- Arguments: positional (`arg1 arg2`) and keyword (`key=value`)
- Quoted strings preserve whitespace
- Escaped quotes supported (`\"`)

## What to watch for

- **Parsing**: Uses `shlex` (POSIX); Windows-style paths with backslashes need quoting
- **Validation**: The parser limits positional argument count and length; keyword arguments are not subject to those two limits, and commands should validate their own values
- **Context**: Commands receive `conversation_id`, `tenant_id`, `user_id` for auth
- **Async**: All commands are async; use `await` for I/O
- **Continuation**: `continue_conversation` is returned to the caller; no current chat adapter consumes it
- **Built-in handlers**: `/goal` implements `create`, `list`, and `complete`; `/goal update` is not implemented. `/compact --llm` is parsed as a positional token by the current parser, so the handler does not enable its LLM flag from that spelling.
- **Hidden commands**: Set `hidden=True` to exclude from `/help` and completions
- **Conversation required**: Set `requires_conversation=False` for global commands
- **Completions**: `get_completions()` returns bounded command-name matches when an embedding caller requests them; no current composer calls it. `help_width` is stored in configuration but is not currently applied to generated help text.
