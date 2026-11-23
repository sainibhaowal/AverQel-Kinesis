"""Sandbox execution tool for DeepSpace.

Provides bounded code execution in Python, SQL, Bash, or PowerShell.
"""

from __future__ import annotations

import base64
from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    array_property,
    define_native_tool,
    string_property,
)

# ============================================================
# Argument Model
# ============================================================


class SandboxExecuteArguments(ToolArguments):
    """Execute code in an isolated sandbox."""

    language: Literal["python", "sql", "bash", "pwsh"]
    code: str = Field(min_length=1, max_length=100_000)
    input: dict[str, Any] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=30)
    file_ids: list[str] | None = Field(default=None, max_length=5)


# ============================================================
# Handler
# ============================================================


async def _sandbox_execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Execute code in the sandbox executor."""
    from app.deepspace.services.sandbox_executor import SandboxExecutorError, execute_sandbox

    try:
        files: list[dict[str, Any]] = []
        file_ids = arguments.get("file_ids") or []
        if file_ids:
            if context.task_store is None:
                raise SandboxExecutorError(
                    "The DeepSpace Library is not available in this runtime."
                )
            for file_id in file_ids:
                library_file = context.task_store.read_workspace_file_for_sandbox(
                    tenant_id=context.auth.tenant_id,
                    user_id=context.auth.user_id,
                    conversation_id=context.conversation_id,
                    file_id=str(file_id),
                    settings=context.settings,
                )
                payload = library_file.get("payload")
                if not isinstance(payload, bytes):
                    raise SandboxExecutorError("The selected Library file payload is invalid.")
                files.append(
                    {
                        "name": str(library_file.get("name") or "file"),
                        "content_type": str(
                            library_file.get("content_type") or "application/octet-stream"
                        ),
                        "data_base64": base64.b64encode(payload).decode("ascii"),
                        "extracted_text": str(library_file.get("extracted_text") or ""),
                    }
                )
        result = await execute_sandbox(
            language=arguments["language"],
            code=arguments["code"],
            input_data=arguments.get("input"),
            timeout_seconds=int(arguments.get("timeout_seconds") or 20),
            files=files,
            settings=context.settings,
        )
        return result
    except SandboxExecutorError as e:
        return {"status": "error", "error_code": "SANDBOX_ERROR", "message": str(e)}
    except Exception as e:
        return {"status": "error", "error_code": "SANDBOX_EXECUTION_FAILED", "message": str(e)}


# ============================================================
# Tool Spec
# ============================================================

SANDBOX_EXECUTE_TOOL = define_native_tool(
    name="sandbox_execute",
    description=(
        "Run bounded Python, read-only SQL, Bash, or PowerShell in AverQel's isolated sandbox. "
        "Use it for calculations, CSV/JSON analysis, and chart preparation; never claim host or network access."
    ),
    properties={
        "language": string_property(
            "The language to execute.",
            enum=["python", "sql", "bash", "pwsh"],
        ),
        "code": string_property("The code to execute.", min_length=1, max_length=100_000),
        "input": {"type": "object", "description": "Optional input data for the script."},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
        "file_ids": array_property(
            "Optional file IDs to make available to the script.", max_items=5
        ),
    },
    required=["language", "code"],
    arguments_model=SandboxExecuteArguments,
    mode="write",
    handler=_sandbox_execute,
)

SANDBOX_TOOLS: list[NativeToolSpec] = [SANDBOX_EXECUTE_TOOL]

__all__ = ["SANDBOX_TOOLS", "SANDBOX_EXECUTE_TOOL", "SandboxExecuteArguments", "_sandbox_execute"]
