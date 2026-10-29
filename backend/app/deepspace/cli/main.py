"""The DeepSpace command-line interface.

The command reads the same HTTP API the web application uses, so a terminal
can list conversations, run a turn, and read the sandbox, plugin, preset,
terminal, and plan state of a conversation without a browser.

The command exits with ``0`` after a successful call, ``1`` when the API
refused the call, and ``2`` when the arguments were not usable.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.deepspace.cli.client import DeepSpaceApiClient, DeepSpaceApiError

__all__ = ["main"]


#: The exit code of a command that read what it was asked for.
EXIT_OK = 0

#: The exit code of a command whose API call failed.
EXIT_API_ERROR = 1

#: The exit code of a command whose arguments were not usable.
EXIT_USAGE_ERROR = 2


def main(argv: list[str] | None = None) -> int:
    """Run one DeepSpace command.

    Args:
        argv: The command-line arguments.  It defaults to the arguments of the
            running process.

    Returns:
        ``0`` when the command succeeded, ``1`` when the API call failed, and
        ``2`` when the arguments were not usable.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)

    # Handle serve-stdio separately (long-running server)
    if str(args.command) == "serve-stdio":
        import asyncio

        from app.deepspace.sdk.server import main as serve_main

        return asyncio.run(serve_main())

    client = DeepSpaceApiClient()
    try:
        payload = _dispatch(args, client)
    except DeepSpaceApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_API_ERROR
    if _wants_json(args):
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(_render(str(args.command), payload))
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser of the command.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="deepspace",
        description="Drive DeepSpace from the terminal.",
    )
    _add_json_flag(parser, default=False)
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.required = True

    def _command(name: str, help_text: str) -> argparse.ArgumentParser:
        """Add one command to the parser.

        Args:
            name: The command name.
            help_text: The one-line description of the command.

        Returns:
            The parser of the command.
        """
        command = commands.add_parser(name, help=help_text, description=help_text)
        _add_json_flag(command, default=argparse.SUPPRESS)
        return command

    _command("chats", "List the conversations.")

    chat = _command("chat", "Show one conversation.")
    chat.add_argument("conversation_id", help="The conversation to show.")

    run = _command("run", "Send a message and print the answer.")
    run.add_argument("conversation_id", help="The conversation that receives the message.")
    run.add_argument("prompt", help="The message to send.")

    _command("sandbox", "Print the sandbox status.")

    _command("plugins", "Print the plugin catalogue.")

    _command("presets", "Print the agent presets.")

    _command("workspaces", "Print the workspaces the account owns.")

    terminals = _command("terminals", "List the live terminal sessions.")
    terminals.add_argument("conversation_id", help="The conversation whose sessions to list.")

    plan = _command("plan", "Print the plan of a conversation.")
    plan.add_argument("conversation_id", help="The conversation whose plan to print.")

    serve = _command("serve-stdio", "Run the stdio JSON-RPC server for SDK clients.")
    serve.add_argument("--debug", action="store_true", help="Enable debug logging")

    return parser


def _add_json_flag(parser: argparse.ArgumentParser, *, default: Any) -> None:
    """Add the machine-readable output flag to one parser.

    Args:
        parser: The parser to add the flag to.
        default: The value the flag carries when it is absent.  A subcommand
            uses ``argparse.SUPPRESS``, so a flag given before the command
            survives the subcommand's own default.
    """
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=default,
        help="Print the API response as JSON instead of text.",
    )


def _wants_json(args: argparse.Namespace) -> bool:
    """Report whether the caller asked for machine-readable output.

    Args:
        args: The parsed arguments.

    Returns:
        ``True`` when the ``--json`` flag was given.
    """
    return bool(getattr(args, "json_output", False))


def _exit_code(exc: SystemExit) -> int:
    """Return the exit code an argument failure carries.

    Args:
        exc: The exception the argument parser raised.

    Returns:
        The exit code, ``2`` for a usage error.
    """
    code = exc.code
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    return EXIT_USAGE_ERROR


def _dispatch(args: argparse.Namespace, client: DeepSpaceApiClient) -> dict[str, Any]:
    """Run the command the arguments select.

    Args:
        args: The parsed arguments.
        client: The API client.

    Returns:
        The payload the command read.

    Raises:
        ValueError: When the command is not one the parser declares.
    """
    command = str(args.command)
    if command == "chats":
        return client.list_chats()
    if command == "chat":
        return client.get_chat(str(args.conversation_id))
    if command == "run":
        return client.send_message(str(args.conversation_id), str(args.prompt))
    if command == "sandbox":
        return client.get_sandbox_status()
    if command == "plugins":
        return client.list_plugins()
    if command == "presets":
        return client.list_presets()
    if command == "workspaces":
        return client.list_workspaces()
    if command == "terminals":
        return client.list_terminals(str(args.conversation_id))
    if command == "plan":
        return client.list_plans(str(args.conversation_id))
    raise ValueError(f"Unknown command: {command}")


def _render(command: str, payload: dict[str, Any]) -> str:
    """Render one payload as text.

    Args:
        command: The command that read the payload.
        payload: The payload the command read.

    Returns:
        The text to print.

    Raises:
        ValueError: When the command is not one the parser declares.
    """
    if command == "chats":
        return _render_chats(payload)
    if command == "chat":
        return _render_chat(payload)
    if command == "run":
        return _render_run(payload)
    if command == "sandbox":
        return _render_sandbox(payload)
    if command == "plugins":
        return _render_plugins(payload)
    if command == "presets":
        return _render_presets(payload)
    if command == "workspaces":
        return _render_workspaces(payload)
    if command == "terminals":
        return _render_terminals(payload)
    if command == "plan":
        return _render_plan(payload)
    raise ValueError(f"Unknown command: {command}")


def _render_chats(payload: dict[str, Any]) -> str:
    """Render the conversation list.

    Args:
        payload: The conversation list response.

    Returns:
        The text to print.
    """
    items = _items(payload.get("items"))
    if not items:
        return "No conversations."
    lines = [
        f"{_text(item.get('id'))}  {_text(item.get('updated_at'))}  "
        f"{_text(item.get('title')) or '(untitled)'}"
        for item in items
    ]
    total = payload.get("total")
    if isinstance(total, int):
        lines.append("")
        lines.append(f"{total} conversation(s).")
    return "\n".join(lines)


def _render_chat(payload: dict[str, Any]) -> str:
    """Render one conversation's messages.

    Args:
        payload: The chat history response.

    Returns:
        The text to print.
    """
    messages = _items(payload.get("messages"))
    if not messages:
        return "No messages."
    blocks = [
        f"{_text(message.get('role')) or 'message'}:\n{_text(message.get('content'))}"
        for message in messages
    ]
    return "\n\n".join(blocks)


def _render_run(payload: dict[str, Any]) -> str:
    """Render the answer of one run.

    Args:
        payload: The answer of the run.

    Returns:
        The text to print.
    """
    return _text(payload.get("content")) or "(the run produced no answer)"


def _render_sandbox(payload: dict[str, Any]) -> str:
    """Render the sandbox status.

    Args:
        payload: The sandbox status response.

    Returns:
        The text to print.
    """
    lines = [
        f"configured:   {_text(payload.get('configured'))}",
        f"selected:     {_text(payload.get('selected'))}",
        f"available:    {_text(payload.get('available'))}",
        f"network:      {_text(payload.get('network'))}",
        f"platform:     {_text(payload.get('platform'))}",
        f"enforcement:  {_text(payload.get('enforcement'))}",
    ]
    error_code = _text(payload.get("error_code"))
    if error_code:
        lines.append(f"error:        {error_code}")
    return "\n".join(lines)


def _render_plugins(payload: dict[str, Any]) -> str:
    """Render the plugin catalogue.

    Args:
        payload: The plugin catalogue response.

    Returns:
        The text to print.
    """
    plugins = _items(payload.get("plugins"))
    if not plugins:
        return "No plugins."
    lines: list[str] = []
    for plugin in plugins:
        identifier = _text(plugin.get("id"))
        name = _text(plugin.get("name")) or identifier
        version = _text(plugin.get("version"))
        default = " (enabled by default)" if plugin.get("enabled_by_default") else ""
        lines.append(f"{identifier}  {name} {version}{default}".rstrip())
        tools = [_text(tool) for tool in _items(plugin.get("tools"))]
        if tools:
            lines.append(f"  tools: {', '.join(tools)}")
    for error in _items(payload.get("errors")):
        lines.append(f"error: {_text(error)}")
    return "\n".join(lines)


def _render_presets(payload: dict[str, Any]) -> str:
    """Render the agent presets.

    Args:
        payload: The preset list response.

    Returns:
        The text to print.
    """
    presets = _items(payload.get("presets"))
    if not presets:
        return "No presets."
    default = _text(payload.get("default"))
    lines: list[str] = []
    for preset in presets:
        identifier = _text(preset.get("id"))
        name = _text(preset.get("name")) or identifier
        marker = " (default)" if identifier == default else ""
        lines.append(f"{identifier}  {name}{marker}")
        description = _text(preset.get("description"))
        if description:
            lines.append(f"  {description}")
    return "\n".join(lines)


def _render_workspaces(payload: dict[str, Any]) -> str:
    """Render the workspaces the account owns.

    Args:
        payload: The workspace catalogue response.

    Returns:
        The text to print.
    """
    workspaces = _items(payload.get("workspaces"))
    if not workspaces:
        return "No workspaces."
    return "\n".join(
        f"{_text(workspace.get('name'))}  id={_text(workspace.get('id'))}  "
        f"{_text(workspace.get('path'))}"
        + ("  (default)" if workspace.get("is_default") is True else "")
        for workspace in workspaces
    )


def _render_terminals(payload: dict[str, Any]) -> str:
    """Render the live terminal sessions.

    Args:
        payload: The terminal session list response.

    Returns:
        The text to print.
    """
    sessions = _items(payload.get("sessions"))
    if not sessions:
        return "No live terminal sessions."
    return "\n".join(
        f"{_text(session.get('session_id'))}  shell={_text(session.get('shell'))}  "
        f"pty={_text(session.get('pty'))}  idle={_text(session.get('idle_seconds'))}s"
        for session in sessions
    )


def _render_plan(payload: dict[str, Any]) -> str:
    """Render the plan of one conversation.

    Args:
        payload: The plan response.

    Returns:
        The text to print.
    """
    lines = [f"plan mode: {'active' if payload.get('active') else 'inactive'}"]
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        lines.append("no plan")
        return "\n".join(lines)
    lines.append(f"title:  {_text(plan.get('title'))}")
    lines.append(f"status: {_text(plan.get('status'))}")
    content = _text(plan.get("content"))
    if content:
        lines.append("")
        lines.append(content)
    return "\n".join(lines)


def _items(value: Any) -> list[dict[str, Any]]:
    """Return the objects one payload field carries.

    Args:
        value: The raw field.

    Returns:
        The objects, ignoring any value that is not one.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: Any) -> str:
    """Return one payload value as text.

    Args:
        value: The raw value.

    Returns:
        The text, empty when the value is absent.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
