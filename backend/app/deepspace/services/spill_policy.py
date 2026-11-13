"""Context-budgeted previews for durable DeepSpace spill artifacts.

The original result remains in the spill store. This module only chooses a
bounded, ordered excerpt for the model, keeping image blocks indivisible when
the result already contains a multimodal ``content`` sequence.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TokenCounter = Callable[[str], int]
_OMISSION_MARKER = "[… omitted …]"


def estimate_text_tokens(value: str) -> int:
    """Estimate tokens conservatively without binding to a model vendor."""
    return max(1, (len(value.encode("utf-8", errors="replace")) + 3) // 4)


@dataclass(frozen=True, slots=True)
class RetainedPreview:
    """An ordered head/tail excerpt and exact omitted-content accounting."""

    preview: str
    omitted_bytes: int
    omitted_images: int = 0


def _fit_text(
    value: str,
    budget: int,
    *,
    tail: bool,
    token_counter: TokenCounter,
) -> str:
    """Find the longest codepoint-safe contiguous edge within a token budget."""
    if budget <= 0 or not value:
        return ""
    low = 0
    high = len(value)
    while low < high:
        count = (low + high + 1) // 2
        candidate = value[len(value) - count :] if tail else value[:count]
        if token_counter(candidate) <= budget:
            low = count
        else:
            high = count - 1
    return value[len(value) - low :] if tail else value[:low]


def retain_text(
    value: str,
    token_budget: int,
    *,
    token_counter: TokenCounter = estimate_text_tokens,
) -> RetainedPreview:
    """Retain both ends of text without splitting Python Unicode codepoints."""
    budget = max(0, int(token_budget))
    if budget == 0:
        return RetainedPreview(
            preview="", omitted_bytes=len(value.encode("utf-8", errors="replace"))
        )
    if token_counter(value) <= budget:
        return RetainedPreview(preview=value, omitted_bytes=0)
    marker_cost = token_counter(f"\n\n{_OMISSION_MARKER}\n\n")
    if budget < marker_cost:
        return RetainedPreview(
            preview="", omitted_bytes=len(value.encode("utf-8", errors="replace"))
        )
    content_budget = max(0, budget - marker_cost)
    head_budget = (content_budget + 1) // 2
    tail_budget = content_budget // 2
    head = _fit_text(value, head_budget, tail=False, token_counter=token_counter)
    remainder = value[len(head) :]
    tail = _fit_text(remainder, tail_budget, tail=True, token_counter=token_counter)
    omitted = remainder[: len(remainder) - len(tail)] if tail else remainder
    if not omitted:
        preview = head + tail
    elif head and tail:
        preview = f"{head}\n\n{_OMISSION_MARKER}\n\n{tail}"
    else:
        preview = f"{head or tail}{_OMISSION_MARKER}" if head or tail else _OMISSION_MARKER
    retained = len(head.encode("utf-8", errors="replace")) + len(
        tail.encode("utf-8", errors="replace")
    )
    total = len(value.encode("utf-8", errors="replace"))
    return RetainedPreview(preview=preview, omitted_bytes=max(0, total - retained))


def retain_content_blocks(
    blocks: Sequence[Mapping[str, Any]],
    token_budget: int,
    *,
    token_counter: TokenCounter = estimate_text_tokens,
    image_token_cost: int = 256,
) -> RetainedPreview:
    """Retain ordered text edges and whole images from a tool content list."""
    normalized = [dict(block) for block in blocks]
    if any(block.get("type") not in {"text", "image"} for block in normalized):
        encoded = json.dumps(normalized, ensure_ascii=False, default=str)
        return retain_text(encoded, token_budget, token_counter=token_counter)

    budget = max(0, int(token_budget))

    def cost(block: Mapping[str, Any]) -> int:
        if block["type"] == "image":
            # Count the serialized image descriptor conservatively. Base64
            # data then costs at least one token per four characters, so a
            # large image is kept in the durable spill and never overwhelms
            # the supposedly small preview.
            serialized = json.dumps(block, ensure_ascii=False, default=str, separators=(",", ":"))
            return max(1, int(image_token_cost), token_counter(serialized))
        return max(1, token_counter(str(block.get("text") or "")))

    estimated_cost = sum(cost(block) for block in normalized)
    if estimated_cost <= budget:
        return RetainedPreview(
            preview=json.dumps(normalized, ensure_ascii=False, default=str), omitted_bytes=0
        )
    marker_cost = token_counter(f"\n\n{_OMISSION_MARKER}\n\n")
    if budget < marker_cost:
        return RetainedPreview(
            preview="",
            omitted_bytes=sum(
                len(str(block.get("text") or "").encode("utf-8", errors="replace"))
                for block in normalized
                if block["type"] == "text"
            ),
            omitted_images=sum(block["type"] == "image" for block in normalized),
        )
    budget = max(0, budget - marker_cost)
    head: list[dict[str, Any]] = []
    tail: list[dict[str, Any]] = []
    first = 0
    last = len(normalized) - 1
    head_budget = (budget + 1) // 2
    tail_budget = budget // 2
    head_chars = 0

    remaining = head_budget
    while first <= last:
        block = normalized[first]
        if cost(block) <= remaining:
            head.append(block)
            remaining -= cost(block)
            first += 1
            continue
        if block["type"] == "text":
            text = _fit_text(
                str(block.get("text") or ""),
                remaining,
                tail=False,
                token_counter=token_counter,
            )
            head_chars = len(text)
            if text:
                head.append({**block, "text": text})
        break

    remaining = tail_budget
    while last >= first:
        block = normalized[last]
        if last == first and block["type"] == "text":
            block = {**block, "text": str(block.get("text") or "")[head_chars:]}
        if cost(block) <= remaining:
            tail.append(block)
            remaining -= cost(block)
            last -= 1
            continue
        if block["type"] == "text":
            text = _fit_text(
                str(block.get("text") or ""),
                remaining,
                tail=True,
                token_counter=token_counter,
            )
            if text:
                tail.append({**block, "text": text})
        break
    tail.reverse()

    retained_blocks = [*head]
    if first <= last:
        retained_blocks.append({"type": "text", "text": _OMISSION_MARKER})
    retained_blocks.extend(tail)
    retained_images = sum(block.get("type") == "image" for block in (*head, *tail))
    total_images = sum(block.get("type") == "image" for block in normalized)
    total_bytes = sum(
        len(str(block.get("text") or "").encode("utf-8", errors="replace"))
        for block in normalized
        if block.get("type") == "text"
    )
    kept_bytes = sum(
        len(str(block.get("text") or "").encode("utf-8", errors="replace"))
        for block in (*head, *tail)
        if block.get("type") == "text"
    )
    return RetainedPreview(
        preview=json.dumps(retained_blocks, ensure_ascii=False, default=str),
        omitted_bytes=max(0, total_bytes - kept_bytes),
        omitted_images=max(0, total_images - retained_images),
    )


def result_preview(payload: Any, serialized: str, token_budget: int) -> RetainedPreview:
    """Build a structured multimodal preview when possible, text otherwise."""
    try:
        if isinstance(payload, Mapping):
            content = payload.get("content")
            if isinstance(content, list) and all(isinstance(block, Mapping) for block in content):
                return retain_content_blocks(content, token_budget)
        if isinstance(payload, list) and all(isinstance(block, Mapping) for block in payload):
            if all(block.get("type") in {"text", "image"} for block in payload):
                return retain_content_blocks(payload, token_budget)
    except (AttributeError, RecursionError, TypeError, ValueError):
        # Structured previews are an optimization. If malformed or cyclic
        # result data defeats that path, retain the already-serialized text.
        pass
    return retain_text(serialized, token_budget)


def take_recovered_images(payload: dict[str, Any]) -> list[str]:
    """Remove internal spill image bytes from a tool result for model input."""
    images = payload.pop("_images_base64", [])
    if not isinstance(images, list):
        return []
    return [image for image in images if isinstance(image, str) and image]


__all__ = [
    "RetainedPreview",
    "estimate_text_tokens",
    "result_preview",
    "retain_content_blocks",
    "retain_text",
    "take_recovered_images",
]
