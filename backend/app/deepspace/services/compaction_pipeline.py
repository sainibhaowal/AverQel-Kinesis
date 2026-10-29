"""The ordered stages that prepare a DeepSpace request for its model window.

DeepSpace already fits a conversation to the model's context window.  Three
stages run before that fit, because all reduce what the request carries
without losing anything the model can still reach:

``summarize_history`` (LLM-based, optional)
    Creates a durable summary of the conversation history using an LLM.
    The summary replaces a range of messages and is stored in the context summary table.

``prune_tool_results``
    A tool result that is large becomes an excerpt plus a reference.  The
    full result stays retrievable through the tool result store, so nothing
    is lost, only deferred.

``offload_images``
    An image block in the history becomes a placeholder once the request
    carries more image data than the route accepts.

All stages are additive: a request that passes through them unchanged is
still a valid request.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from app.auth.dependencies import AuthContext
from app.core.config import Settings
from app.deepspace.services.session_events import SessionEventStore

# Compaction event types
COMPACTION_START = "compaction/start"
COMPACTION_SUMMARY = "compaction/summary"
COMPACTION_END = "compaction/end"

# LLM summarization settings
DEFAULT_SUMMARY_MAX_TOKENS = 4000
SUMMARY_PROMPT = """Summarize the following conversation history concisely.
Focus on:
- Key decisions and outcomes
- Important facts and context
- Current objectives and open questions
- Tool results that affect future steps

Write a summary that would allow a new agent to continue the work.
Keep it under {max_tokens} tokens.
"""

#: The character ceiling a tool result may hold before it is pruned.
DEFAULT_PRUNE_THRESHOLD = 8_192

#: The number of characters kept from the start of a pruned result.
PRUNE_HEAD_CHARS = 4_096

#: The number of characters kept from the end of a pruned result.
PRUNE_TAIL_CHARS = 1_024

#: The marker that replaces a pruned tool result.
PRUNE_MARKER = "[tool result truncated; the full result is retrievable by its reference]"

#: The number of image blocks one request may carry before they are offloaded.
DEFAULT_IMAGE_BUDGET = 4

#: The marker that replaces an offloaded image block.
IMAGE_MARKER = "[image removed from the request to fit the model's image budget]"

#: The number of recent messages the basic stage always keeps.
DEFAULT_KEEP_RECENT_MESSAGES = 24

#: The marker that replaces a contiguous run of elided older messages.
ELISION_MARKER = "[earlier conversation elided to fit the model's context]"


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """What the pipeline changed in one request."""

    #: The messages the request should carry.
    messages: list[dict[str, Any]]
    #: The number of older messages the basic stage elided.
    elided_messages: int = 0
    #: The number of tool results that were pruned.
    pruned_tool_results: int = 0
    #: The number of image blocks that were offloaded.
    offloaded_images: int = 0
    #: The references the pruned results are retrievable by.
    result_refs: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Report whether the pipeline changed the request."""
        return self.elided_messages > 0 or self.pruned_tool_results > 0 or self.offloaded_images > 0


def _is_tool_result(message: dict[str, Any]) -> bool:
    """Report whether a message carries a tool result."""
    return str(message.get("role") or "") == "tool"


def _result_text(message: dict[str, Any]) -> str:
    """Read a tool result's text."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return ""


def _pruned_text(text: str, *, threshold: int = DEFAULT_PRUNE_THRESHOLD) -> str:
    """Render the excerpt a pruned tool result carries.

    The excerpt never exceeds the threshold, so pruning always shortens the
    result it applies to.  The head keeps the beginning of the result and
    the tail keeps its end, because a result's conclusion usually sits at
    the end.

    Args:
        text: The result text.
        threshold: The character ceiling the excerpt must fit inside.

    Returns:
        The excerpt.
    """
    marker = f"\n{PRUNE_MARKER}"
    separator = "\n…\n"
    budget = max(200, int(threshold)) - len(marker)
    head_budget = min(PRUNE_HEAD_CHARS, max(100, int(budget * 0.8)))
    tail_budget = min(PRUNE_TAIL_CHARS, max(0, budget - head_budget - len(separator)))
    head = text[:head_budget]
    if tail_budget <= 0 or len(text) <= head_budget:
        return f"{head}{marker}"
    tail = text[-tail_budget:]
    return f"{head}{separator}{tail}{marker}"


def prune_tool_results(
    messages: list[dict[str, Any]],
    *,
    threshold: int = DEFAULT_PRUNE_THRESHOLD,
) -> tuple[list[dict[str, Any]], int]:
    """Shorten the oversized tool results in a request.

    Args:
        messages: The request's messages.
        threshold: The character ceiling a tool result may hold.

    Returns:
        The shortened messages and the number of results that were pruned.
    """
    if threshold <= 0:
        return list(messages), 0
    pruned = 0
    result: list[dict[str, Any]] = []
    for message in messages:
        if not _is_tool_result(message):
            result.append(message)
            continue
        text = _result_text(message)
        if len(text) <= threshold:
            result.append(message)
            continue
        replacement = dict(message)
        replacement["content"] = _pruned_text(text, threshold=threshold)
        result.append(replacement)
        pruned += 1
    return result, pruned


def _image_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect the image blocks a message carries."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict) and item.get("type") == "image"]


def offload_images(
    messages: list[dict[str, Any]],
    *,
    image_budget: int = DEFAULT_IMAGE_BUDGET,
) -> tuple[list[dict[str, Any]], int]:
    """Remove the images that exceed the request's image budget.

    The newest images are kept, because they are the ones the current turn
    is about, and the oldest are removed first.

    Args:
        messages: The request's messages.
        image_budget: The largest number of image blocks one request may carry.

    Returns:
        The shortened messages and the number of images that were offloaded.
    """
    if image_budget < 0:
        return list(messages), 0
    total = sum(len(_image_blocks(message)) for message in messages)
    if total <= image_budget:
        return list(messages), 0
    excess = total - image_budget
    offloaded = 0
    result: list[dict[str, Any]] = []
    for message in messages:
        blocks = _image_blocks(message)
        if not blocks or offloaded >= excess:
            result.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            result.append(message)
            continue
        kept: list[Any] = []
        for item in content:
            if offloaded < excess and isinstance(item, dict) and item.get("type") == "image":
                offloaded += 1
                kept.append({"type": "text", "text": IMAGE_MARKER})
                continue
            kept.append(item)
        replacement = dict(message)
        replacement["content"] = kept
        result.append(replacement)
    return result, offloaded


def elide_stale_messages(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT_MESSAGES,
) -> tuple[list[dict[str, Any]], int]:
    """Replace a contiguous run of older messages with one marker.

    The stage keeps every system message, the opening user request, the
    last ``keep_recent`` messages, and complete tool exchanges.  The cut
    moves back to the assistant message that owns a tool result, so a kept
    result never loses its call.

    Args:
        messages: The request's messages.
        keep_recent: The number of trailing messages to keep.

    Returns:
        The messages to carry and the number of messages that were elided.
    """
    if keep_recent <= 0 or len(messages) <= keep_recent + 1:
        return messages, 0
    keep_from = max(1, len(messages) - keep_recent)
    while keep_from > 1 and str(messages[keep_from].get("role") or "") == "tool":
        keep_from -= 1
    if keep_from <= 1:
        return messages, 0
    prefix = messages[:keep_from]
    kept_prefix = [message for message in prefix if str(message.get("role") or "") == "system"]
    # The opening request anchors everything that follows, so it survives
    # even when every other older message is elided.
    first_user = next(
        (message for message in prefix if str(message.get("role") or "") == "user"),
        None,
    )
    if first_user is not None and first_user not in kept_prefix:
        kept_prefix.append(first_user)
    elided = len(prefix) - len(kept_prefix)
    if elided == 0:
        return messages, 0
    marker = {"role": "system", "content": ELISION_MARKER}
    return [*kept_prefix, marker, *messages[keep_from:]], elided


def compact_request(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT_MESSAGES,
    prune_threshold: int = DEFAULT_PRUNE_THRESHOLD,
    image_budget: int = DEFAULT_IMAGE_BUDGET,
) -> CompactionOutcome:
    """Run the pipeline over one request.

    The stages run in order: elide stale messages, prune oversized tool
    results, then offload surplus images.  Each stage only shortens the
    request, so a later stage never reintroduces what an earlier one removed.

    Args:
        messages: The request's messages.
        keep_recent: The number of trailing messages the basic stage keeps.
        prune_threshold: The character ceiling a tool result may hold.
        image_budget: The largest number of image blocks one request may carry.

    Returns:
        The outcome, carrying the messages the request should carry.
    """
    elided_messages, elided = elide_stale_messages(messages, keep_recent=keep_recent)
    pruned_messages, pruned = prune_tool_results(elided_messages, threshold=prune_threshold)
    final_messages, offloaded = offload_images(pruned_messages, image_budget=image_budget)
    return CompactionOutcome(
        messages=final_messages,
        elided_messages=elided,
        pruned_tool_results=pruned,
        offloaded_images=offloaded,
    )


def compact_tool_result_payload(
    payload: Any, *, threshold: int = DEFAULT_PRUNE_THRESHOLD
) -> tuple[str, bool]:
    """Render a tool result for the model, shortening an oversized one.

    Args:
        payload: The tool's result.
        threshold: The character ceiling the rendered result may hold.

    Returns:
        The rendered text and whether it was shortened.
    """
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    if len(text) <= threshold:
        return text, False
    return _pruned_text(text, threshold=threshold), True


# =============================================================================
# LLM-based summarization stage
# =============================================================================


async def summarize_history(
    history: list[dict[str, Any]],
    *,
    provider_registry: Any,
    model_selection: Any,
    settings: Settings,
    max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
    signal: asyncio.Event | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Summarize conversation history using an LLM.

    Args:
        history: The full conversation history (messages).
        provider_registry: The provider registry for LLM calls.
        model_selection: The model selection service.
        settings: Deployment settings.
        max_tokens: Maximum tokens for the summary.
        signal: Cancellation signal.

    Returns:
        A tuple of (new_history_with_summary, summary_metadata).
    """
    if not getattr(settings, "deepspace_compaction_llm_enabled", False):
        return history, {}

    # Check if there's enough history to summarize
    if len(history) < 10:  # Need enough history to be worth summarizing
        return history, {}

    # Separate system messages and user/assistant messages
    system_messages = [m for m in history if m.get("role") == "system"]
    user_assistant_messages = [m for m in history if m.get("role") in ("user", "assistant")]
    tool_messages = [m for m in history if m.get("role") == "tool"]

    # If too few non-system messages, skip
    if len(user_assistant_messages) < 6:
        return history, {}

    # Build the text to summarize (keep tool results out of summary to save tokens)
    summary_text = "\n".join(
        f"{m.get('role', '').upper()}: {m.get('content', '')[:500]}"
        for m in user_assistant_messages[:-4]  # Don't summarize the most recent messages
    )

    if not summary_text.strip():
        return history, {}

    # Build summarization prompt
    prompt = SUMMARY_PROMPT.format(max_tokens=max_tokens) + "\n\n" + summary_text

    try:
        # Get provider
        model_name = getattr(settings, "deepspace_compaction_llm_model", None)
        if not model_name:
            # Use default model selection
            provider = await model_selection.select(
                tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                user_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                task_type="summarization",
            )
        else:
            provider = provider_registry.get_provider(model_name)

        if not provider:
            return history, {}

        # Generate summary
        result = await provider.generate(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.3,
        )

        summary = result.get("text", "").strip()
        if not summary:
            return history, {}

        # Create new history with summary
        # Keep system messages, summary, and recent messages
        recent_count = min(4, len(user_assistant_messages))
        new_history = []
        new_history.extend(system_messages)
        new_history.append(
            {
                "role": "system",
                "content": f"Previous conversation summary:\n{summary}",
            }
        )
        new_history.extend(user_assistant_messages[-recent_count:])
        new_history.extend(tool_messages[-recent_count * 2 :])  # Keep related tool results

        summary_metadata = {
            "summary": summary,
            "original_message_count": len(history),
            "summarized_message_count": len(user_assistant_messages) - recent_count,
            "summary_tokens_estimate": len(summary.split()) * 1.3,
            "provider": model_name or "auto",
        }

        return new_history, summary_metadata

    except Exception as e:
        # If summarization fails, return original history
        import logging

        logging.getLogger(__name__).warning("History summarization failed: %s", e)
        return history, {}


# =============================================================================
# Compaction event emission
# =============================================================================


async def emit_compaction_start(
    event_store: SessionEventStore,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    turn: int,
) -> None:
    """Emit compaction/start event."""
    event_store.append(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        event_type=COMPACTION_START,
        payload={"turn": turn},
        run_id=run_id,
    )


async def emit_compaction_summary(
    event_store: SessionEventStore,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    turn: int,
    summary: str,
    shadowed_range: tuple[int, int],
    shadowed_seqs: list[int],
    shadowed_token_count: int,
    provider: str,
    model: str,
    max_tokens: int | None = None,
    usage: dict | None = None,
) -> None:
    """Emit compaction/summary event."""
    event_store.append(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        event_type=COMPACTION_SUMMARY,
        payload={
            "summary": summary,
            "shadowed_range": shadowed_range,
            "shadowed_seqs": shadowed_seqs,
            "shadowed_token_count": shadowed_token_count,
            "provider": provider,
            "model": model,
            "max_tokens": max_tokens,
            "usage": usage,
        },
        run_id=run_id,
        turn=turn,
    )


async def emit_compaction_end(
    event_store: SessionEventStore,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    turn: int,
    error: str | None = None,
) -> None:
    """Emit compaction/end event."""
    event_store.append(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        event_type=COMPACTION_END,
        payload={"turn": turn, "error": error},
        run_id=run_id,
    )


# =============================================================================
# Full compaction pipeline with LLM summarization
# =============================================================================


async def compact_request_full(
    messages: list[dict[str, Any]],
    *,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    turn: int,
    keep_recent: int = DEFAULT_KEEP_RECENT_MESSAGES,
    prune_threshold: int = DEFAULT_PRUNE_THRESHOLD,
    image_budget: int = DEFAULT_IMAGE_BUDGET,
    settings: Settings,
    provider_registry: Any,
    model_selection: Any,
    event_store: SessionEventStore,
    db: Any,
) -> CompactionOutcome:
    """Run the full compaction pipeline including LLM summarization.

    Stages (in order):
    1. LLM summarization (optional, if enabled and enough history)
    2. Elide stale messages
    3. Prune oversized tool results
    4. Offload surplus images
    """
    # Stage 0: LLM summarization (before other stages)
    summary_metadata = {}
    if getattr(settings, "deepspace_compaction_llm_enabled", False):
        # Emit compaction/start
        await emit_compaction_start(event_store, auth, conversation_id, run_id, turn)

        messages, summary_metadata = await summarize_history(
            messages,
            provider_registry=provider_registry,
            model_selection=model_selection,
            settings=settings,
            max_tokens=getattr(
                settings, "deepspace_compaction_llm_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS
            ),
        )

        if summary_metadata:
            # Emit compaction/summary with metadata
            await emit_compaction_summary(
                event_store=event_store,
                auth=auth,
                conversation_id=conversation_id,
                run_id=run_id,
                turn=turn,
                summary=summary_metadata.get("summary", ""),
                shadowed_range=(0, summary_metadata.get("summarized_message_count", 0)),
                shadowed_seqs=[],
                shadowed_token_count=summary_metadata.get("summary_tokens_estimate", 0),
                provider=summary_metadata.get("provider", "unknown"),
                model=summary_metadata.get("model", "unknown"),
                max_tokens=getattr(
                    settings, "deepspace_compaction_llm_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS
                ),
            )

        # Emit compaction/end
        await emit_compaction_end(event_store, auth, conversation_id, run_id, turn)

    # Stage 1: Elide stale messages
    messages, elided = elide_stale_messages(messages, keep_recent=keep_recent)

    # Stage 2: Prune oversized tool results
    messages, pruned = prune_tool_results(messages, threshold=prune_threshold)

    # Stage 3: Offload surplus images
    messages, offloaded = offload_images(messages, image_budget=image_budget)

    return CompactionOutcome(
        messages=messages,
        elided_messages=elided,
        pruned_tool_results=pruned,
        offloaded_images=offloaded,
    )
