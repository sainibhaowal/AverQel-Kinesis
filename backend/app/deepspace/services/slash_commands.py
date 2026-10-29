"""Slash-command registry for DeepSpace.

Provides a centralized registry for slash commands (/, /compact, /goal, etc.)
with argument parsing, completion, and help.
"""

from __future__ import annotations

import logging
import shlex
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SlashCommandConfig:
    """Slash command registry configuration."""

    enabled: bool = True
    max_args: int = 20
    max_arg_length: int = 1024
    completion_limit: int = 50
    help_width: int = 80


# ──────────────────────────────────────────────────────────────────────
# Command Types
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SlashCommandArg:
    """A parsed slash command argument."""

    name: str
    value: str
    quoted: bool = False


@dataclass(frozen=True, slots=True)
class SlashCommandContext:
    """Context passed to slash command handlers."""

    conversation_id: UUID | None
    tenant_id: UUID
    user_id: UUID
    raw_input: str
    command: str
    args: list[SlashCommandArg]
    kwargs: dict[str, str]


@dataclass(frozen=True, slots=True)
class SlashCommandResult:
    """Result of a slash command execution."""

    success: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    continue_conversation: bool = True


class SlashCommand(ABC):
    """Base class for slash commands."""

    name: str
    description: str = ""
    usage: str = ""
    aliases: tuple[str, ...] = ()
    hidden: bool = False
    requires_conversation: bool = True

    @abstractmethod
    async def execute(self, context: SlashCommandContext) -> SlashCommandResult:
        """Execute the command."""
        pass

    def get_help(self) -> str:
        """Get help text for the command."""
        parts = [f"/{self.name}"]
        if self.usage:
            parts.append(self.usage)
        if self.description:
            parts.append(f"- {self.description}")
        return " ".join(parts)

    def get_completions(self, partial: str) -> list[str]:
        """Get argument completions for the command."""
        return []


class FunctionSlashCommand(SlashCommand):
    """A slash command backed by a function."""

    def __init__(
        self,
        name: str,
        func: Callable[[SlashCommandContext], SlashCommandResult | Any],
        description: str = "",
        usage: str = "",
        aliases: tuple[str, ...] = (),
        hidden: bool = False,
        requires_conversation: bool = True,
        completer: Callable[[str], list[str]] | None = None,
    ):
        self.name = name
        self._func = func
        self.description = description
        self.usage = usage
        self.aliases = aliases
        self.hidden = hidden
        self.requires_conversation = requires_conversation
        self._completer = completer

    async def execute(self, context: SlashCommandContext) -> SlashCommandResult:
        result = await self._func(context)
        if isinstance(result, SlashCommandResult):
            return result
        return SlashCommandResult(success=True, message=str(result))

    def get_completions(self, partial: str) -> list[str]:
        if self._completer:
            return self._completer(partial)
        return []


# ──────────────────────────────────────────────────────────────────────
# Parser
# ──────────────────────────────────────────────────────────────────────


class SlashCommandParser:
    """Parses slash command input."""

    def __init__(self, config: SlashCommandConfig):
        self.config = config

    def parse(self, input: str) -> tuple[str, list[SlashCommandArg], dict[str, str]] | None:
        """Parse a slash command string.

        Returns (command, args, kwargs) or None if not a slash command.
        """
        input = input.strip()
        if not input.startswith("/"):
            return None

        # Remove leading slash
        input = input[1:]

        # Split by whitespace, respecting quotes
        try:
            tokens = shlex.split(input, posix=True)
        except ValueError:
            # Unmatched quote - treat as single command
            tokens = [input]

        if not tokens:
            return None

        command = tokens[0].lower()
        args = []
        kwargs = {}

        for token in tokens[1:]:
            if "=" in token and not token.startswith("="):
                key, value = token.split("=", 1)
                kwargs[key] = value
            else:
                args.append(SlashCommandArg(name=f"arg{len(args)}", value=token))

        if len(args) > self.config.max_args:
            raise ValueError(f"Too many arguments (max {self.config.max_args})")

        for arg in args:
            if len(arg.value) > self.config.max_arg_length:
                raise ValueError(f"Argument too long (max {self.config.max_arg_length} chars)")

        return command, args, kwargs


# ──────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────


class SlashCommandRegistry:
    """Registry of slash commands."""

    def __init__(self, config: SlashCommandConfig):
        self.config = config
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}
        self.parser = SlashCommandParser(config)
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register built-in slash commands."""
        self.register(
            FunctionSlashCommand(
                name="help",
                func=self._cmd_help,
                description="Show available slash commands",
                usage="[command]",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="compact",
                func=self._cmd_compact,
                description="Trigger session compaction",
                usage="[--llm]",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="goal",
                func=self._cmd_goal,
                description="Manage conversation goals",
                usage="<create|list|update|complete> [args...]",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="permission",
                func=self._cmd_permission,
                description="Manage permission mode",
                usage="<auto|ask|deny>",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="feedback",
                func=self._cmd_feedback,
                description="Send feedback about this session",
                usage="<good|bad> [message]",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="plan",
                func=self._cmd_plan,
                description="Enter or exit plan mode",
                usage="[on|off]",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="persona",
                func=self._cmd_persona,
                description="Show deployment persona",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="skills",
                func=self._cmd_skills,
                description="List workspace skills",
                usage="[query]",
            )
        )
        self.register(
            FunctionSlashCommand(
                name="export",
                func=self._cmd_export,
                description="Export session log",
                usage="[jsonl|json] [--max=N]",
            )
        )

    def register(self, command: SlashCommand) -> None:
        """Register a slash command."""
        self._commands[command.name] = command
        for alias in command.aliases:
            self._aliases[alias] = command.name

    def unregister(self, name: str) -> bool:
        """Unregister a command."""
        if name in self._commands:
            cmd = self._commands.pop(name)
            for alias in cmd.aliases:
                self._aliases.pop(alias, None)
            return True
        return False

    def get(self, name: str) -> SlashCommand | None:
        """Get a command by name or alias."""
        name = name.lower()
        if name in self._commands:
            return self._commands[name]
        if name in self._aliases:
            return self._commands[self._aliases[name]]
        return None

    def list_commands(self, include_hidden: bool = False) -> list[SlashCommand]:
        """List all registered commands."""
        return [cmd for cmd in self._commands.values() if include_hidden or not cmd.hidden]

    def get_completions(self, input: str, limit: int | None = None) -> list[str]:
        """Get command completions for partial input."""
        limit = limit or self.config.completion_limit
        results = []

        # Match command names
        if input.startswith("/"):
            prefix = input[1:].lower()
            for cmd in self.list_commands():
                if cmd.name.startswith(prefix):
                    results.append(f"/{cmd.name}")
                for alias in cmd.aliases:
                    if alias.startswith(prefix):
                        results.append(f"/{alias}")

        return results[:limit]

    async def execute(self, input: str, context: SlashCommandContext) -> SlashCommandResult:
        """Execute a slash command from raw input."""
        parsed = self.parser.parse(input)
        if not parsed:
            return SlashCommandResult(
                success=False,
                message="Not a slash command (must start with /)",
            )

        command_name, args, kwargs = parsed
        command = self.get(command_name)
        if not command:
            return SlashCommandResult(
                success=False,
                message=f"Unknown command: /{command_name}. Type /help for available commands.",
            )

        if command.requires_conversation and not context.conversation_id:
            return SlashCommandResult(
                success=False,
                message="This command requires an active conversation.",
            )

        # Build arg objects
        cmd_args = [SlashCommandArg(name=f"arg{i}", value=a.value) for i, a in enumerate(args)]
        ctx = SlashCommandContext(
            conversation_id=context.conversation_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            raw_input=input,
            command=command.name,
            args=cmd_args,
            kwargs=kwargs,
        )

        try:
            return await command.execute(ctx)
        except Exception as e:
            logger.exception("Slash command execution failed")
            return SlashCommandResult(
                success=False,
                message=f"Command failed: {e}",
            )

    # ──────────────────────────────────────────────────────────────────
    # Built-in Command Implementations
    # ──────────────────────────────────────────────────────────────────

    async def _cmd_help(self, context: SlashCommandContext) -> SlashCommandResult:
        commands = self.list_commands()
        if not commands:
            return SlashCommandResult(success=True, message="No commands available.")

        if context.args:
            cmd = self.get(context.args[0].value)
            if cmd:
                return SlashCommandResult(success=True, message=cmd.get_help())
            return SlashCommandResult(
                success=False, message=f"Unknown command: {context.args[0].value}"
            )

        lines = ["Available slash commands:"]
        for cmd in sorted(commands, key=lambda c: c.name):
            if not cmd.hidden:
                lines.append(f"  /{cmd.name} - {cmd.description or 'No description'}")
        return SlashCommandResult(success=True, message="\n".join(lines))

    async def _cmd_compact(self, context: SlashCommandContext) -> SlashCommandResult:
        use_llm = "--llm" in context.kwargs or "-l" in context.kwargs
        return SlashCommandResult(
            success=True,
            message=f"Compaction triggered (LLM: {use_llm})",
            data={"action": "compact", "llm": context.kwargs.get("llm", False)},
        )

    async def _cmd_goal(self, context: SlashCommandContext) -> SlashCommandResult:
        subcommand = context.args[0].value if context.args else "list"
        if subcommand == "create":
            name = context.kwargs.get("name") or (
                context.args[1].value if len(context.args) > 1 else ""
            )
            return SlashCommandResult(
                success=True,
                message=f"Goal created: {name}",
                data={"action": "goal_create", "name": name},
            )
        elif subcommand == "list":
            return SlashCommandResult(
                success=True, message="Goals:\n- (no goals)", data={"action": "goal_list"}
            )
        elif subcommand == "complete":
            goal_id = context.args[1].value if len(context.args) > 1 else ""
            return SlashCommandResult(
                success=True,
                message=f"Goal completed: {goal_id}",
                data={"action": "goal_complete", "goal_id": goal_id},
            )
        return SlashCommandResult(
            success=True, message="Usage: /goal <create|list|complete> [args...]"
        )

    async def _cmd_permission(self, context: SlashCommandContext) -> SlashCommandResult:
        mode = context.args[0].value if context.args else "auto"
        if mode not in ("auto", "ask", "deny"):
            return SlashCommandResult(
                success=False, message="Invalid mode. Use: auto, ask, or deny"
            )
        return SlashCommandResult(
            success=True, message=f"Permission mode set to: {mode}", data={"permission_mode": mode}
        )

    async def _cmd_feedback(self, context: SlashCommandContext) -> SlashCommandResult:
        rating = context.args[0].value if context.args else "good"
        message = " ".join(a.value for a in context.args[1:]) if len(context.args) > 1 else ""
        return SlashCommandResult(
            success=True,
            message=f"Feedback recorded: {rating}",
            data={"rating": rating, "message": message},
        )

    async def _cmd_plan(self, context: SlashCommandContext) -> SlashCommandResult:
        state = context.args[0].value if context.args else "toggle"
        if state not in ("on", "off", "toggle"):
            return SlashCommandResult(success=False, message="Usage: /plan [on|off|toggle]")
        return SlashCommandResult(
            success=True, message=f"Plan mode: {state}", data={"plan_mode": state}
        )

    async def _cmd_persona(self, context: SlashCommandContext) -> SlashCommandResult:
        return SlashCommandResult(
            success=True,
            message="Deployment persona: (not configured)",
            data={"persona_configured": False},
        )

    async def _cmd_skills(self, context: SlashCommandContext) -> SlashCommandResult:
        query = context.args[0].value if context.args else None
        return SlashCommandResult(
            success=True, message="Skills: (none)", data={"query": query, "skills": []}
        )

    async def _cmd_export(self, context: SlashCommandContext) -> SlashCommandResult:
        fmt = context.args[0].value if context.args else "jsonl"
        max_events = int(context.kwargs.get("max", "10000"))
        if fmt not in ("jsonl", "json"):
            return SlashCommandResult(success=False, message="Format must be 'jsonl' or 'json'")
        return SlashCommandResult(
            success=True,
            message=f"Export started ({fmt}, max {max_events})",
            data={"format": fmt, "max_events": max_events},
        )


# ──────────────────────────────────────────────────────────────────────
# Registry Factory
# ──────────────────────────────────────────────────────────────────────

_SLASH_REGISTRY: SlashCommandRegistry | None = None


def get_slash_command_registry(settings: Settings | None = None) -> SlashCommandRegistry:
    """Get or create the global slash command registry."""
    global _SLASH_REGISTRY
    if _SLASH_REGISTRY is None:
        cfg = SlashCommandConfig(
            enabled=getattr(settings or get_settings(), "deepspace_slash_commands_enabled", True),
            max_args=getattr(settings or get_settings(), "deepspace_slash_max_args", 20),
            max_arg_length=getattr(
                settings or get_settings(), "deepspace_slash_max_arg_length", 1024
            ),
            completion_limit=getattr(
                settings or get_settings(), "deepspace_slash_completion_limit", 50
            ),
            help_width=getattr(settings or get_settings(), "deepspace_slash_help_width", 80),
        )
        _SLASH_REGISTRY = SlashCommandRegistry(cfg)
    return _SLASH_REGISTRY


def init_slash_command_registry(settings: Settings) -> SlashCommandRegistry:
    """Initialize the slash command registry."""
    global _SLASH_REGISTRY
    cfg = SlashCommandConfig(
        enabled=getattr(settings, "deepspace_slash_commands_enabled", True),
        max_args=getattr(settings, "deepspace_slash_max_args", 20),
        max_arg_length=getattr(settings, "deepspace_slash_max_arg_length", 1024),
        completion_limit=getattr(settings, "deepspace_slash_completion_limit", 50),
        help_width=getattr(settings, "deepspace_slash_help_width", 80),
    )
    _SLASH_REGISTRY = SlashCommandRegistry(cfg)
    return _SLASH_REGISTRY


__all__ = [
    "SlashCommandConfig",
    "SlashCommandArg",
    "SlashCommandContext",
    "SlashCommandResult",
    "SlashCommand",
    "FunctionSlashCommand",
    "SlashCommandParser",
    "SlashCommandRegistry",
    "get_slash_command_registry",
    "init_slash_command_registry",
]
