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
DEFAULT_PRECOMPUTE_LEAD_TOKENS = 60_000
PRECOMPUTE_MAX_FAILURES = 2
LEGACY_HEADING = "## Additional Context"

SUMMARY_TEMPLATE = """You MUST use this format for your response (you may omit sections that aren't applicable). Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Requirements
- [constraints, preferences, requirements, and scope boundaries stated by the user, or "(none)"]

## Decisions
- [decisions already made and why, or "(none)"]

## Work State
Break the objective into smaller goals and report which are completed, which are being worked on, and which are blocked.
### Completed
- [goals that have been completed; otherwise "(none)"]

### Active
- [goals currently being worked on; otherwise "(none)"]

### Blocked
- [anything blocking progress, and why; otherwise "(none)"]

## Next Move
1. [ordered list of next actions, or "(none)"]

## Relevant Files
List the files and directories, other than the current working directory, that another agent would need to open to continue this work. Include at most 15, most important first. Do not list every file that was read or changed. Include paths outside the current working directory when relevant. If none, write "(none)".
- `[file or directory path]`: [brief reason it matters]

## Important Context
- [facts the next agent cannot continue without and cannot easily find on its own; or "(none)"]
</template>"""

SUMMARY_RULES = """Rules:
- Keep each section concise. Use terse, single-line bullets, not prose paragraphs or nested lists.
- Prefer short references over detailed restatement. It is fine to leave out information the next agent can recover from the code or the files listed above.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers.
- Carry forward only user questions or requests that remain unanswered or require further action. Do not repeat ones that newer history has answered or resolved. Preserve exact wording when carrying one forward.
- Preserve consequential workflow state, including whether changes are uncommitted, committed, pushed, under review, or merged.
- Do not mention the summary process or that context was compacted."""

SUMMARY_PROMPT = (
    "You MUST summarize the conversation above into a structured summary that will be given "
    "to another agent to resume the work.\n\n" + SUMMARY_TEMPLATE + "\n\n" + SUMMARY_RULES
)


def build_summary_prompt(*, update: bool = False, legacy: bool = False) -> str:
    """Build the OpenCode-aligned structured compaction prompt.

    Args:
        update: Whether an existing checkpoint summary is being incrementally updated.
        legacy: Whether the existing checkpoint used an older unstructured format.

    Returns:
        The complete summarization instruction prompt.
    """
    shared = [
        (
            "Summarize only what the user and the assistant said and did. Leave out instructions "
            "and setup the assistant was given rather than told by the user: repository conventions, "
            "instruction files such as AGENTS.md, and environment details like the session ID. "
            "The next agent receives current versions of all of these separately."
        ),
        SUMMARY_TEMPLATE,
        SUMMARY_RULES,
        "Do not continue the task or call tools.",
        "Return only the structured summary in the requested format. Do not include a preamble, explanation, or other commentary.",
    ]
    if not update:
        return "\n\n".join(
            [
                "You MUST summarize the conversation above into a structured summary that will be given to another agent to resume the work.",
                *shared,
            ]
        )
    parts = [
        "Update the existing checkpoint in the conversation above into one consolidated summary.",
    ]
    if legacy:
        parts.append(
            "The existing checkpoint was written with an earlier format that recorded far more detail "
            "than this one asks for. Rewrite it at the level of detail described below rather than "
            "carrying its detail forward. Keep its requirements, decisions, and open questions; "
            "they came from earlier conversation with the user."
        )
    parts.extend(
        [
            "Newer history always takes precedence over the existing checkpoint. Preserve previous "
            "information unless newer history clearly contradicts, supersedes, resolves, or makes it stale. "
            "If something is no longer relevant to continuing the work, you may remove it.",
            "Incorporate newer requirements, decisions, progress, and context. Reconcile Work State and "
            "Next Move: move completed work out of Active, remove resolved blockers and answered questions, "
            "and preserve unresolved or pending work.",
            "Return only the updated Markdown sections. Do not reproduce wrapper tags from the previous checkpoint.",
            *shared,
        ]
    )
    return "\n\n".join(parts)


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


def _extract_existing_checkpoint(history: list[dict[str, Any]]) -> str | None:
    """Return the latest structured compaction checkpoint from history, if any."""
    for message in reversed(history):
        if str(message.get("role") or "") != "system":
            continue
        content = str(message.get("content") or "")
        if "Previous conversation summary:" in content or "## Objective" in content:
            return content
    return None


def _render_history_for_summary(
    messages: list[dict[str, Any]],
    *,
    existing_checkpoint: str | None = None,
) -> str:
    """Render conversation messages (including tool call/result excerpts) for summarization."""
    lines: list[str] = []
    if existing_checkpoint:
        lines.append(f"<existing_checkpoint>\n{existing_checkpoint}\n</existing_checkpoint>")
    for message in messages:
        role = str(message.get("role") or "").upper()
        if not role or role == "SYSTEM":
            continue
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            rendered = "\n".join(part for part in text_parts if part)
        else:
            rendered = str(content or "")
        rendered = rendered.strip()
        if not rendered:
            continue
        limit = 800 if role in ("USER", "ASSISTANT") else 400
        if len(rendered) > limit:
            rendered = rendered[:limit] + "…"
        lines.append(f"{role}: {rendered}")
    return "\n".join(lines)


async def _resolve_summary_provider(
    *,
    provider_registry: Any,
    model_selection: Any,
    settings: Settings,
    provider: Any = None,
) -> tuple[Any | None, str]:
    """Resolve a ChatProvider and model name for compaction summarization."""
    model_name = (
        getattr(settings, "deepspace_compaction_llm_model", None)
        or getattr(settings, "llm_model", None)
        or "default"
    )
    if provider is not None:
        return provider, str(model_name)
    if provider_registry is not None and hasattr(provider_registry, "get_chat_provider"):
        try:
            return provider_registry.get_chat_provider(), str(model_name)
        except Exception:  # noqa: BLE001
            pass
    if model_selection is not None and hasattr(model_selection, "select"):
        selected = await model_selection.select(
            tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
            task_type="summarization",
        )
        return selected, str(model_name)
    return None, str(model_name)


async def _invoke_summary_llm(
    *,
    provider: Any,
    model_name: str,
    prompt: str,
    max_tokens: int,
    settings: Settings,
) -> str:
    """Invoke a sync or async ChatProvider to produce a structured checkpoint summary."""
    from app.providers.services.types import ChatGenerateRequest

    generate_fn = getattr(provider, "generate", None)
    if generate_fn is None:
        return ""
    request = ChatGenerateRequest(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=max_tokens,
        base_url=getattr(settings, "llm_api_base_url", None) or "",
        api_key=getattr(settings, "llm_api_key", None),
        stream=False,
    )
    if asyncio.iscoroutinefunction(generate_fn):
        result = await generate_fn(request)
    else:
        result = await asyncio.to_thread(generate_fn, request)
    if isinstance(result, dict):
        return str(result.get("text") or result.get("content") or "").strip()
    return str(getattr(result, "content", None) or getattr(result, "text", None) or "").strip()


async def summarize_history(
    history: list[dict[str, Any]],
    *,
    provider_registry: Any = None,
    model_selection: Any = None,
    settings: Settings,
    max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
    signal: asyncio.Event | None = None,
    provider: Any = None,
    existing_summary: str | None = None,
    model_name: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Summarize conversation history using an LLM into the structured checkpoint format."""
    if not getattr(settings, "deepspace_compaction_llm_enabled", True):
        return history, {}

    if signal is not None and signal.is_set():
        return history, {}

    if len(history) < 10:
        return history, {}

    system_messages = [m for m in history if m.get("role") == "system"]
    non_system_messages = [m for m in history if m.get("role") in ("user", "assistant", "tool")]
    user_assistant_messages = [m for m in history if m.get("role") in ("user", "assistant")]

    if len(user_assistant_messages) < 6:
        return history, {}

    recent_count = min(4, len(user_assistant_messages))
    cutoff_msg = user_assistant_messages[-recent_count]
    cutoff_idx = next(
        (i for i, m in enumerate(non_system_messages) if m is cutoff_msg),
        max(0, len(non_system_messages) - recent_count),
    )
    older_messages = non_system_messages[:cutoff_idx]
    kept_tail = non_system_messages[cutoff_idx:]

    checkpoint = existing_summary or _extract_existing_checkpoint(history)
    summary_text = _render_history_for_summary(older_messages, existing_checkpoint=checkpoint)
    if not summary_text.strip():
        return history, {}

    is_update = bool(checkpoint)
    is_legacy = bool(checkpoint and LEGACY_HEADING in checkpoint)
    instruction = build_summary_prompt(update=is_update, legacy=is_legacy)
    prompt = f"{summary_text}\n\n{instruction}"

    try:
        resolved_provider, resolved_model = await _resolve_summary_provider(
            provider_registry=provider_registry,
            model_selection=model_selection,
            settings=settings,
            provider=provider,
        )
        if model_name:
            resolved_model = model_name
        if not resolved_provider:
            return history, {}

        summary = await _invoke_summary_llm(
            provider=resolved_provider,
            model_name=resolved_model,
            prompt=prompt,
            max_tokens=max_tokens,
            settings=settings,
        )
        if not summary:
            return history, {}

        preserved_system = [
            m
            for m in system_messages
            if "Previous conversation summary:" not in str(m.get("content") or "")
        ]
        new_history = [
            *preserved_system,
            {
                "role": "system",
                "content": f"Previous conversation summary:\n{summary}",
            },
            *kept_tail,
        ]

        summary_metadata = {
            "summary": summary,
            "original_message_count": len(history),
            "summarized_message_count": len(older_messages),
            "summary_tokens_estimate": int(len(summary.split()) * 1.3),
            "provider": resolved_model,
            "model": resolved_model,
            "incremental_update": is_update,
        }

        return new_history, summary_metadata

    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("History summarization failed: %s", e)
        return history, {}


@dataclass(slots=True)
class PrecomputedSummary:
    """A background-computed structured compaction checkpoint held until compaction is due."""

    conversation_id: uuid.UUID
    source_message_count: int
    summary: str
    metadata: dict[str, Any]


class CompactionPrecomputer:
    """Schedules background structured summarization before the context ceiling is hit."""

    def __init__(self) -> None:
        self._held: dict[uuid.UUID, PrecomputedSummary] = {}
        self._in_flight: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._failures: dict[uuid.UUID, int] = {}

    def get_held(self, conversation_id: uuid.UUID) -> PrecomputedSummary | None:
        return self._held.get(conversation_id)

    def take(
        self, conversation_id: uuid.UUID, *, min_source_messages: int = 0
    ) -> PrecomputedSummary | None:
        held = self._held.pop(conversation_id, None)
        if held is None or held.source_message_count < min_source_messages:
            return None
        self._failures.pop(conversation_id, None)
        return held

    def invalidate(self, conversation_id: uuid.UUID) -> None:
        self._held.pop(conversation_id, None)
        task = self._in_flight.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()

    def should_precompute(
        self,
        *,
        conversation_id: uuid.UUID,
        used_tokens: int,
        context_limit: int | None,
        lead_tokens: int = DEFAULT_PRECOMPUTE_LEAD_TOKENS,
    ) -> bool:
        if not context_limit or context_limit <= 0:
            return False
        if self._failures.get(conversation_id, 0) >= PRECOMPUTE_MAX_FAILURES:
            return False
        if conversation_id in self._in_flight:
            return False
        threshold = max(0, context_limit - max(1, min(lead_tokens, context_limit // 2)))
        return used_tokens >= threshold

    def maybe_schedule(
        self,
        *,
        conversation_id: uuid.UUID,
        history: list[dict[str, Any]],
        used_tokens: int,
        context_limit: int | None,
        settings: Settings,
        provider: Any = None,
        provider_registry: Any = None,
        model_selection: Any = None,
        existing_summary: str | None = None,
        model_name: str | None = None,
    ) -> bool:
        if not getattr(settings, "deepspace_compaction_llm_enabled", True):
            return False
        if not getattr(settings, "deepspace_compaction_background_precompute_enabled", True):
            return False
        lead = int(
            getattr(
                settings,
                "deepspace_compaction_precompute_lead_tokens",
                DEFAULT_PRECOMPUTE_LEAD_TOKENS,
            )
        )
        if not self.should_precompute(
            conversation_id=conversation_id,
            used_tokens=used_tokens,
            context_limit=context_limit,
            lead_tokens=lead,
        ):
            return False
        held = self._held.get(conversation_id)
        if held is not None and held.source_message_count >= len(history):
            return False

        snapshot = list(history)

        async def _run() -> None:
            try:
                _, metadata = await summarize_history(
                    snapshot,
                    provider_registry=provider_registry,
                    model_selection=model_selection,
                    settings=settings,
                    max_tokens=int(
                        getattr(
                            settings,
                            "deepspace_compaction_llm_max_tokens",
                            DEFAULT_SUMMARY_MAX_TOKENS,
                        )
                    ),
                    provider=provider,
                    existing_summary=existing_summary,
                    model_name=model_name,
                )
                summary_text = str(metadata.get("summary") or "").strip()
                if summary_text:
                    self._held[conversation_id] = PrecomputedSummary(
                        conversation_id=conversation_id,
                        source_message_count=len(snapshot),
                        summary=summary_text,
                        metadata=metadata,
                    )
                    self._failures.pop(conversation_id, None)
                else:
                    self._failures[conversation_id] = self._failures.get(conversation_id, 0) + 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self._failures[conversation_id] = self._failures.get(conversation_id, 0) + 1
            finally:
                self._in_flight.pop(conversation_id, None)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._in_flight[conversation_id] = loop.create_task(_run())
        return True


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
    usage: dict[str, Any] | None = None,
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
    provider_registry: Any = None,
    model_selection: Any = None,
    event_store: SessionEventStore,
    db: Any,
    provider: Any = None,
    existing_summary: str | None = None,
    model_name: str | None = None,
    precomputed_summary: PrecomputedSummary | None = None,
) -> CompactionOutcome:
    """Run the full compaction pipeline including LLM summarization."""
    summary_metadata: dict[str, Any] = {}
    if getattr(settings, "deepspace_compaction_llm_enabled", True):
        await emit_compaction_start(event_store, auth, conversation_id, run_id, turn)

        if precomputed_summary is not None and precomputed_summary.summary.strip():
            system_messages = [
                m
                for m in messages
                if m.get("role") == "system"
                and "Previous conversation summary:" not in str(m.get("content") or "")
            ]
            non_system = [m for m in messages if m.get("role") in ("user", "assistant", "tool")]
            kept_tail = non_system[-max(4, min(keep_recent, len(non_system))) :]
            messages = [
                *system_messages,
                {
                    "role": "system",
                    "content": f"Previous conversation summary:\n{precomputed_summary.summary}",
                },
                *kept_tail,
            ]
            summary_metadata = {
                **precomputed_summary.metadata,
                "summary": precomputed_summary.summary,
                "precomputed": True,
            }
        else:
            messages, summary_metadata = await summarize_history(
                messages,
                provider_registry=provider_registry,
                model_selection=model_selection,
                settings=settings,
                max_tokens=getattr(
                    settings, "deepspace_compaction_llm_max_tokens", DEFAULT_SUMMARY_MAX_TOKENS
                ),
                provider=provider,
                existing_summary=existing_summary,
                model_name=model_name,
            )

        if summary_metadata:
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
