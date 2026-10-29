"""Spill tools for DeepSpace.

A tool result that is too large to carry in the model context is offloaded to
durable, content-addressed storage.  The model receives a preview plus a
reference; ``spill_read`` reads the complete result back when the preview is
not enough.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.spill_store import SpillStore, SpillStoreError
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    integer_property,
    string_property,
)

#: The largest number of characters one spill read returns.
MAX_SPILL_READ_CHARS = 200_000

#: The largest number of characters one spill read's offset may skip.
MAX_SPILL_OFFSET = 5_000_000


class SpillReadArguments(ToolArguments):
    """Read a spilled tool result back."""

    spill_ref: str = Field(min_length=64, max_length=64)
    offset: int | None = Field(default=None, ge=0, le=MAX_SPILL_OFFSET)
    max_characters: int | None = Field(default=None, ge=100, le=MAX_SPILL_READ_CHARS)


async def _spill_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Read one spilled result the model was given a reference for."""
    reference = str(arguments["spill_ref"]).strip().lower()
    store = SpillStore(context.settings)
    try:
        text = store.get(tenant_id=context.auth.tenant_id, sha256=reference)
    except SpillStoreError as exc:
        return {"status": "unavailable", "message": str(exc), "spill_ref": reference}
    offset = int(arguments.get("offset") or 0)
    limit = min(
        MAX_SPILL_READ_CHARS,
        max(100, int(arguments.get("max_characters") or MAX_SPILL_READ_CHARS)),
    )
    window = text[offset : offset + limit]
    return {
        "status": "ok",
        "spill_ref": reference,
        "text": window,
        "offset": offset,
        "total_characters": len(text),
        "truncated": offset + len(window) < len(text),
        "next_offset": offset + len(window) if offset + len(window) < len(text) else None,
    }


SPILL_DESCRIPTION = (
    "Read a spilled tool result. A tool result too large for the context window "
    "is offloaded to durable storage and reported with a spill_ref; use "
    "spill_read with that reference to read the complete result back."
)

SPILL_TOOLS: list[NativeToolSpec] = [
    define_native_tool(
        name="spill_read",
        description=SPILL_DESCRIPTION,
        properties={
            "spill_ref": string_property(
                "The reference a spilled tool result reported.",
                min_length=64,
                max_length=64,
            ),
            "offset": integer_property(
                "The character offset to start reading from.", minimum=0, maximum=MAX_SPILL_OFFSET
            ),
            "max_characters": integer_property(
                "The largest number of characters to return.",
                minimum=100,
                maximum=MAX_SPILL_READ_CHARS,
            ),
        },
        required=["spill_ref"],
        arguments_model=SpillReadArguments,
        mode="read",
        handler=_spill_read,
    ),
]

__all__ = ["SPILL_TOOLS", "SpillReadArguments"]
