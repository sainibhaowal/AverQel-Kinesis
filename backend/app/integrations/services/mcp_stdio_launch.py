"""Validated launch metadata for approved local STDIO MCP providers."""

from __future__ import annotations

from typing import Any


def stdio_launch_details(entry: Any) -> dict[str, Any] | None:
    """Return the exact argv launch description, or ``None`` if it is invalid.

    STDIO commands are executed as argument vectors, never through a shell. The
    caller can show these values to the user before it requests a launch.
    """
    if str(getattr(entry, "transport", "") or "").casefold() != "stdio":
        return None
    raw = getattr(entry, "raw_metadata", None)
    raw = raw if isinstance(raw, dict) else {}
    server = raw.get("server") if isinstance(raw.get("server"), dict) else raw
    command = server.get("command")
    raw_args = server.get("args", [])
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > 512
        or "\x00" in command
        or not isinstance(raw_args, list)
        or len(raw_args) > 128
    ):
        return None
    args: list[str] = []
    for item in raw_args:
        if not isinstance(item, str) or len(item) > 4096 or "\x00" in item:
            return None
        args.append(item)
    return {"command": command, "args": args}
