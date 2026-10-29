"""Programmatic tool calling tools for DeepSpace.

``run_code`` lets the model write one program that calls host tools as
ordinary function calls.  The program runs in a confined child process, so
a failing program cannot take the turn down, and the host runs every
requested tool through the same policy and tenant scope as a direct call.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
    integer_property,
    string_property,
)

#: The largest program one call may carry, in characters.
MAX_PROGRAM_CHARS = 100_000

#: The largest number of tools one program may name.
MAX_PROGRAM_TOOLS = 16


class RunCodeArguments(ToolArguments):
    """Run one program that calls DeepSpace tools."""

    code: str = Field(min_length=1, max_length=MAX_PROGRAM_CHARS)
    tools: list[str] | None = Field(default=None, max_length=MAX_PROGRAM_TOOLS)
    timeout_seconds: int | None = Field(default=None, ge=5, le=300)
    language: Literal["python", "javascript"] = "python"


async def _run_code(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run one program and return what it printed and returned."""
    # Lazy import to avoid circular dependency
    from app.deepspace.services.ptc_runtime import (
        ProgramRuntimeError,
        run_program,
    )

    program = str(arguments["code"])
    language = str(arguments.get("language") or "python")
    requested = [str(name).strip() for name in (arguments.get("tools") or []) if str(name).strip()]

    try:
        outcome = await run_program(
            context=context,
            program=program,
            tool_names=requested,
            timeout_seconds=arguments.get("timeout_seconds"),
            language=language,
        )
    except ProgramRuntimeError as exc:
        return {
            "status": "unavailable",
            "execution": "programmatic_tool_calling",
            "message": str(exc),
        }
    return {
        **outcome.as_dict(),
        "execution": "programmatic_tool_calling",
        "language": language,
    }


RUN_CODE_DESCRIPTION = (
    "Run one program that calls DeepSpace tools as ordinary function calls. "
    "The program receives a 'tool' function: call tool('web_search', {'query': '...'}) "
    "and it returns that tool's result. The program runs in a confined sandbox with no "
    "network, so every piece of data it needs must come from a tool call. Print what you "
    "want to see and return a value to hand it back. Use this for a loop over many items "
    "or a multi-step calculation that would otherwise need one provider round trip per step. "
    "Supports Python and JavaScript (Node.js) languages."
)

RUN_CODE_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="run_code",
        description=RUN_CODE_DESCRIPTION,
        properties={
            "code": string_property(
                "The program to run. It receives a 'tool' function and returns a value.",
                min_length=1,
                max_length=MAX_PROGRAM_CHARS,
            ),
            "language": {
                "type": "string",
                "enum": ["python", "javascript"],
                "description": "The programming language (python or javascript)",
                "default": "python",
            },
            "tools": array_property(
                "The tool names the program may call. Omit to let the program call none.",
                max_items=MAX_PROGRAM_TOOLS,
            ),
            "timeout_seconds": integer_property(
                "The wall-clock budget for the program.", minimum=5, maximum=300
            ),
        },
        required=["code"],
        arguments_model=RunCodeArguments,
        mode="write",
        handler=_run_code,
    ),
]

__all__ = ["RUN_CODE_TOOLS", "RunCodeArguments"]
