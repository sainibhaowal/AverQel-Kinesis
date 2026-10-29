"""Manual compaction tool for DeepSpace."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.compaction_pipeline import (
    COMPACTION_START,
)
from app.deepspace.services.session_events import SessionEventStore
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    ToolContext,
    define_native_tool,
    require_db,
)


class CompactArguments(ToolArguments):
    """Manually trigger session compaction with optional LLM summary."""

    mode: Literal["auto", "llm"] = Field(
        default="auto",
        description="Compaction mode: 'auto' uses deterministic pipeline, 'llm' forces LLM summarization",
    )


async def _compact(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Manually trigger session compaction."""
    mode = str(arguments.get("mode") or "auto")

    db = require_db(context)
    event_store = SessionEventStore(db)

    # Get the conversation's current messages
    # This would need to fetch from the conversation history
    # For now, we trigger a compaction on the next turn by marking the conversation
    from app.deepspace.models.conversation import Conversation

    conv = db.execute(
        __import__("sqlalchemy", fromlist=["select"])
        .select(Conversation)
        .where(Conversation.id == context.conversation_id)
    ).scalar_one_or_none()

    if not conv:
        return {"status": "error", "message": "Conversation not found"}

    # Mark the conversation for compaction on next turn
    # In practice, this would trigger the compaction pipeline immediately
    # For now, we return a success status indicating the compaction was requested

    if mode == "llm":
        # Emit compaction events for LLM mode
        await event_store.append(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            event_type=COMPACTION_START,
            payload={"turn": 0, "manual": True, "mode": "llm"},
            run_id=context.run_id,
        )

        return {
            "status": "ok",
            "message": "LLM compaction requested. The next turn will run with LLM summarization enabled.",
            "mode": "llm",
        }
    else:
        # Emit compaction events for auto mode
        await event_store.append(
            tenant_id=context.auth.tenant_id,
            user_id=context.auth.user_id,
            conversation_id=context.conversation_id,
            event_type=COMPACTION_START,
            payload={"turn": 0, "manual": True, "mode": "auto"},
            run_id=context.run_id,
        )

        return {
            "status": "ok",
            "message": "Compaction requested. The next turn will run the deterministic compaction pipeline.",
            "mode": "auto",
        }


COMPACT_TOOLS = [
    define_native_tool(
        name="compact",
        description=(
            "Manually trigger session compaction. Use 'auto' for deterministic pipeline "
            "(elide, prune, offload) or 'llm' to force LLM-based summarization. "
            "The compaction runs at the start of the next turn."
        ),
        properties={
            "mode": {
                "type": "string",
                "enum": ["auto", "llm"],
                "description": "Compaction mode",
                "default": "auto",
            },
        },
        arguments_model=CompactArguments,
        mode="write",
        handler=_compact,
        required=[],
    ),
]

__all__ = ["COMPACT_TOOLS", "CompactArguments"]
