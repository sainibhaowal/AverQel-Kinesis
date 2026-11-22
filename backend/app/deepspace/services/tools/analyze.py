"""Document analysis tool for DeepSpace.

Compares authorized Library documents by their extracted text.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

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


class AnalyzeArguments(ToolArguments):
    """Compare documents for differences."""

    operation: Literal["compare_documents"] = "compare_documents"
    left_file_id: str | None = Field(default=None, max_length=80)
    right_file_id: str | None = Field(default=None, max_length=80)
    file_ids: list[str] | None = Field(default=None, min_length=2, max_length=5)
    max_characters: int | None = Field(default=None, ge=100, le=100_000)

    @model_validator(mode="after")
    def require_comparison_targets(self) -> AnalyzeArguments:
        if self.file_ids or (self.left_file_id and self.right_file_id):
            return self
        raise ValueError("analyze(compare_documents) requires file_ids or a left/right file pair")


# ============================================================
# Handler
# ============================================================


async def _analyze(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Compare documents and return structured diff."""
    from app.deepspace.services.tools.diff import diff_text

    try:
        file_ids = []
        if arguments.get("file_ids"):
            file_ids = arguments["file_ids"]
        elif arguments.get("left_file_id") and arguments.get("right_file_id"):
            file_ids = [arguments["left_file_id"], arguments["right_file_id"]]
        else:
            return {
                "status": "error",
                "error_code": "INVALID_ARGUMENTS",
                "message": "No files specified for comparison",
            }

        max_chars = arguments.get("max_characters")

        store = context.task_store
        if store is None:
            return {
                "status": "error",
                "error_code": "STORE_UNAVAILABLE",
                "message": "The Library store is not available in this runtime.",
            }

        # Read all files
        contents = {}
        for fid in file_ids:
            try:
                result = store.read_workspace_file(
                    tenant_id=context.auth.tenant_id,
                    user_id=context.auth.user_id,
                    conversation_id=context.conversation_id,
                    file_id=str(fid),
                )
                content = result.get("content") if isinstance(result, dict) else None
                if content is None:
                    return {
                        "status": "error",
                        "error_code": "FILE_NOT_FOUND",
                        "message": f"File {fid} not found or inaccessible",
                    }
                if max_chars and isinstance(content, str):
                    content = content[:max_chars]
                contents[fid] = content
            except Exception:
                return {
                    "status": "error",
                    "error_code": "FILE_NOT_FOUND",
                    "message": f"File {fid} not found or inaccessible",
                }

        # Compare pairwise
        if len(file_ids) == 2:
            diff = diff_text(contents[file_ids[0]], contents[file_ids[1]])
            return {"status": "ok", "comparison": diff, "files": file_ids}
        else:
            # Multi-file comparison
            comparisons = {}
            for i in range(len(file_ids)):
                for j in range(i + 1, len(file_ids)):
                    key = f"{file_ids[i]}_vs_{file_ids[j]}"
                    comparisons[key] = diff_text(contents[file_ids[i]], contents[file_ids[j]])
            return {"status": "ok", "comparisons": comparisons, "files": file_ids}

    except Exception as e:
        return {"status": "error", "error_code": "ANALYZE_FAILED", "message": str(e)}


# ============================================================
# Tool Spec
# ============================================================

ANALYZE_TOOL = define_native_tool(
    name="analyze",
    description=(
        "Compare two or more authorized Library documents by their exact extracted text "
        "and report the differences. This is a deterministic document analysis operation."
    ),
    properties={
        "operation": string_property("The analysis operation.", enum=["compare_documents"]),
        "left_file_id": string_property("Left file ID for pairwise comparison.", max_length=80),
        "right_file_id": string_property("Right file ID for pairwise comparison.", max_length=80),
        "file_ids": array_property(
            "Array of file IDs for multi-file comparison (2-5).", min_items=2, max_items=5
        ),
        "max_characters": {"type": "integer", "minimum": 100, "maximum": 100_000},
    },
    required=["operation"],
    arguments_model=AnalyzeArguments,
    mode="read",
    handler=_analyze,
)

ANALYZE_TOOLS: list[NativeToolSpec] = [ANALYZE_TOOL]

__all__ = ["ANALYZE_TOOLS", "ANALYZE_TOOL", "AnalyzeArguments", "_analyze"]
