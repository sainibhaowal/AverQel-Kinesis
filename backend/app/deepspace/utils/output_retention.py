"""
Output Retention Library for DeepSpace.

Port of @deepseek-ai/dsh-output-retention to Python.
Provides bounded retention for model-facing output with exact omission metadata.

Features:
- ItemRetainer: bounds ordered logical units (head strategy)
- TextRetainer: bounds byte-oriented streams (head/tail/headTail, UTF-8 boundary preservation)
- Standardized omission notices with tool recovery guidance
- Safe UTF-16/UTF-8 truncation
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

T = TypeVar("T")


# ============================================================================
# Omission Metadata
# ============================================================================


@dataclass(frozen=True)
class OmittedNone:
    kind: Literal["none"] = "none"

    def __post_init__(self) -> None:
        if self.kind != "none":
            raise ValueError("OmittedNone.kind must be 'none'")


@dataclass(frozen=True)
class OmittedExact:
    count: int
    kind: Literal["exact"] = "exact"

    def __post_init__(self) -> None:
        if self.kind != "exact":
            raise ValueError("OmittedExact.kind must be 'exact'")
        if self.count < 0:
            raise ValueError("OmittedExact.count must be non-negative")


@dataclass(frozen=True)
class OmittedUnknown:
    kind: Literal["unknown"] = "unknown"

    def __post_init__(self) -> None:
        if self.kind != "unknown":
            raise ValueError("OmittedUnknown.kind must be 'unknown'")


Omitted = OmittedNone | OmittedExact | OmittedUnknown


@dataclass(frozen=True)
class PushDecision:
    """Result of a single push() call."""

    kept: bool
    truncated: bool


# ============================================================================
# Item Retainer (logical units)
# ============================================================================


@dataclass(frozen=True)
class ItemRetentionStrategy:
    """Strategy for item retention. Only 'head' in v1."""

    max_items: int
    kind: Literal["head"] = "head"

    def __post_init__(self) -> None:
        if self.kind != "head":
            raise ValueError("ItemRetentionStrategy.kind must be 'head'")


@dataclass(frozen=True)
class RetainedItems(Generic[T]):
    """Final result for ordered logical units."""

    items: list[T]
    truncated: bool
    seen: int
    kept: int
    omitted: Omitted


class ItemRetainer(Generic[T]):
    """
    Bounds an ordered stream of logical units, keeping the first `max_items`.

    Callers push all observed units; the retainer tracks exact omission count.
    Grouping, sorting, and per-unit preview truncation stay OUTSIDE the retainer.
    """

    def __init__(self, strategy: ItemRetentionStrategy) -> None:
        if not isinstance(strategy.max_items, int) or strategy.max_items < 0:
            raise ValueError("max_items must be a non-negative integer")
        self._max_items = strategy.max_items
        self._items: list[T] = []
        self._seen = 0
        self._omitted_count = 0

    def push(self, item: T) -> PushDecision:
        """Offer one unit. Kept when below max_items; otherwise dropped and counted."""
        self._seen += 1
        if len(self._items) < self._max_items:
            self._items.append(item)
            return PushDecision(kept=True, truncated=False)
        self._omitted_count += 1
        return PushDecision(kept=False, truncated=True)

    def finish(self) -> RetainedItems[T]:
        """Finalize and report what was kept and omitted."""
        truncated = self._omitted_count > 0
        return RetainedItems(
            items=self._items,
            truncated=truncated,
            seen=self._seen,
            kept=len(self._items),
            omitted=(
                OmittedExact(kind="exact", count=self._omitted_count)
                if truncated
                else OmittedNone(kind="none")
            ),
        )


# ============================================================================
# Text Retainer (byte streams)
# ============================================================================

TextRetentionKind = Literal["head", "tail", "headTail"]


@dataclass(frozen=True)
class TextRetentionStrategyHead:
    max_bytes: int
    kind: Literal["head"] = "head"

    def __post_init__(self) -> None:
        if self.kind != "head":
            raise ValueError("TextRetentionStrategyHead.kind must be 'head'")


@dataclass(frozen=True)
class TextRetentionStrategyTail:
    max_bytes: int
    kind: Literal["tail"] = "tail"

    def __post_init__(self) -> None:
        if self.kind != "tail":
            raise ValueError("TextRetentionStrategyTail.kind must be 'tail'")


@dataclass(frozen=True)
class TextRetentionStrategyHeadTail:
    head_bytes: int
    tail_bytes: int
    kind: Literal["headTail"] = "headTail"

    def __post_init__(self) -> None:
        if self.kind != "headTail":
            raise ValueError("TextRetentionStrategyHeadTail.kind must be 'headTail'")


TextRetentionStrategy = (
    TextRetentionStrategyHead | TextRetentionStrategyTail | TextRetentionStrategyHeadTail
)


@dataclass(frozen=True)
class RetainedText:
    """Final result for text streams."""

    text: str
    truncated: bool
    omitted_bytes: Omitted


class TextRetainer:
    """
    Bounds a byte-oriented text stream with head/tail/headTail strategies.

    UTF-8 boundaries are preserved at each cut. Memory is bounded by
    prefix_cap + tail_cap + one chunk.
    """

    def __init__(self, strategy: TextRetentionStrategy) -> None:
        if strategy.kind == "head":
            if not isinstance(strategy.max_bytes, int) or strategy.max_bytes < 0:
                raise ValueError("max_bytes must be a non-negative integer")
            self._prefix_cap = strategy.max_bytes
            self._suffix_cap = 0
        elif strategy.kind == "tail":
            if not isinstance(strategy.max_bytes, int) or strategy.max_bytes < 0:
                raise ValueError("max_bytes must be a non-negative integer")
            self._prefix_cap = 0
            self._suffix_cap = strategy.max_bytes
        elif strategy.kind == "headTail":
            if not isinstance(strategy.head_bytes, int) or strategy.head_bytes < 0:
                raise ValueError("head_bytes must be a non-negative integer")
            if not isinstance(strategy.tail_bytes, int) or strategy.tail_bytes < 0:
                raise ValueError("tail_bytes must be a non-negative integer")
            self._prefix_cap = strategy.head_bytes
            self._suffix_cap = strategy.tail_bytes
        else:
            raise ValueError(f"Unknown strategy kind: {strategy.kind}")

        self._prefix_chunks: list[bytes] = []
        self._prefix_held = 0
        self._suffix_chunks: list[bytes] = []
        self._suffix_held = 0
        self._total = 0

    @staticmethod
    def _trim_trailing_partial_utf8(data: bytes) -> bytes:
        """Remove trailing incomplete UTF-8 sequence."""
        i = len(data) - 1
        while i >= 0 and (data[i] & 0xC0) == 0x80 and len(data) - i <= 3:
            i -= 1
        if i < 0:
            return data
        lead = data[i]
        if lead < 0x80:
            expected = 1
        elif lead < 0xE0:
            expected = 2
        elif lead < 0xF0:
            expected = 3
        elif lead < 0xF8:
            expected = 4
        else:
            return data
        if len(data) - i < expected:
            return data[:i]
        return data

    @staticmethod
    def _trim_leading_continuation_utf8(data: bytes) -> bytes:
        """Remove leading continuation bytes."""
        i = 0
        while i < len(data) and (data[i] & 0xC0) == 0x80:
            i += 1
        return data[i:]

    def _concat(self, chunks: list[bytes]) -> bytes:
        if not chunks:
            return b""
        if len(chunks) == 1:
            return chunks[0]
        total = sum(len(c) for c in chunks)
        result = bytearray(total)
        offset = 0
        for chunk in chunks:
            result[offset : offset + len(chunk)] = chunk
            offset += len(chunk)
        return bytes(result)

    def push(self, chunk: bytes | str) -> PushDecision:
        """Offer a chunk of bytes or string (UTF-8 encoded)."""
        if isinstance(chunk, str):
            data = chunk.encode("utf-8")
        else:
            data = chunk

        before_total = self._total
        self._total += len(data)

        # Prefix: take only up to cap
        room = max(0, self._prefix_cap - self._prefix_held)
        take = min(room, len(data))
        if take > 0:
            self._prefix_chunks.append(data[:take])
            self._prefix_held += take

        # Suffix: append whole chunk, then drop leading chunks that slid out
        if self._suffix_cap > 0:
            self._suffix_chunks.append(data)
            self._suffix_held += len(data)
            while self._suffix_chunks:
                head = self._suffix_chunks[0]
                if self._suffix_held - len(head) >= self._suffix_cap:
                    self._suffix_chunks.pop(0)
                    self._suffix_held -= len(head)
                else:
                    break
            # Trim excess from head chunk if it exceeds suffix_cap
            if self._suffix_chunks and self._suffix_held > self._suffix_cap:
                excess = self._suffix_held - self._suffix_cap
                self._suffix_chunks[0] = self._suffix_chunks[0][excess:]
                self._suffix_held -= excess

        # Check if this chunk caused any omission
        dropped = self._omitted_at(self._total) > self._omitted_at(before_total)
        return PushDecision(
            kept=not dropped,
            truncated=self._omitted_at(self._total) > 0,
        )

    def _omitted_at(self, total: int) -> int:
        prefix_len = min(total, self._prefix_cap)
        suffix_len = min(total - prefix_len, self._suffix_cap)
        return total - prefix_len - suffix_len

    def finish(self) -> RetainedText:
        """Finalize: decode retained prefix/suffix with UTF-8 boundary trimming."""
        prefix_len = min(self._total, self._prefix_cap)
        suffix_len = min(self._total - prefix_len, self._suffix_cap)

        prefix_bytes = self._concat(self._prefix_chunks)[:prefix_len]
        suffix_bytes = self._concat(self._suffix_chunks)[self._suffix_held - suffix_len :]

        budget_omitted = self._omitted_at(self._total)

        if budget_omitted > 0:
            # Real gap exists: trim each side to UTF-8 boundary
            kept_prefix = self._trim_trailing_partial_utf8(prefix_bytes)
            kept_suffix = self._trim_leading_continuation_utf8(suffix_bytes)
            text = kept_prefix.decode("utf-8", errors="replace") + kept_suffix.decode(
                "utf-8", errors="replace"
            )
        else:
            # No gap: prefix and suffix are adjacent, decode as one
            text = (prefix_bytes + suffix_bytes).decode("utf-8", errors="replace")

        # Boundary trimming also omits bytes from the returned text.
        if budget_omitted > 0:
            retained_bytes = len(kept_prefix) + len(kept_suffix)
        else:
            retained_bytes = len(prefix_bytes) + len(suffix_bytes)
        omitted = self._total - retained_bytes
        truncated = omitted > 0

        return RetainedText(
            text=text,
            truncated=truncated,
            omitted_bytes=(
                OmittedExact(kind="exact", count=omitted) if truncated else OmittedNone(kind="none")
            ),
        )


# ============================================================================
# Retention Notice & Formatting
# ============================================================================


@dataclass(frozen=True)
class RetentionNotice:
    """Neutral retention outcome facts for formatting."""

    scope: str  # Tool/scope label (e.g., "grep", "bash stdout")
    strategy: TextRetentionKind  # "head" | "tail" | "headTail"
    unit: Literal["items", "bytes", "chars", "lines"]
    limit: int | dict[str, int]  # max_items, max_bytes, or {head, tail}
    kept: int
    omitted: Omitted


def describe_omitted(omitted: Any, unit: str) -> str:
    """
    Standardized omission wording — library-owned.
    'exact' prints count; 'unknown' prints no count; 'none' is empty.
    """
    if isinstance(omitted, OmittedNone):
        return ""
    if isinstance(omitted, OmittedExact):
        return f"Omitted {omitted.count} {unit}."
    if isinstance(omitted, OmittedUnknown):
        return f"More {unit} were omitted."
    return ""


def format_retention_notice(
    notice: RetentionNotice,
    recovery: Callable[[RetentionNotice], str],
) -> str:
    """
    Combine library omission clause with tool recovery guidance.
    Either half may be empty; joined with single space.
    """
    parts = [
        describe_omitted(notice.omitted, notice.unit),
        recovery(notice),
    ]
    return " ".join(p for p in parts if p)


def truncate_without_splitting_surrogate_pair(text: str, max_chars: int) -> str:
    """
    Cap text at max_chars UTF-16 code units without lone high surrogate.
    A cut inside a surrogate pair drops the unpaired half.
    """
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    if len(encoded) <= max_chars * 2:
        return text
    capped = encoded[: max_chars * 2]
    if len(capped) >= 2:
        last_unit = int.from_bytes(capped[-2:], "little")
        if 0xD800 <= last_unit <= 0xDBFF:
            capped = capped[:-2]
    return capped.decode("utf-16-le", errors="surrogatepass")


# ============================================================================
# Convenience Factory Functions
# ============================================================================


def create_item_retainer(max_items: int) -> ItemRetainer[Any]:
    """Create an ItemRetainer with head strategy."""
    return ItemRetainer(ItemRetentionStrategy(kind="head", max_items=max_items))


def create_text_retainer_head(max_bytes: int) -> TextRetainer:
    """Create a TextRetainer keeping first max_bytes."""
    return TextRetainer(TextRetentionStrategyHead(kind="head", max_bytes=max_bytes))


def create_text_retainer_tail(max_bytes: int) -> TextRetainer:
    """Create a TextRetainer keeping last max_bytes."""
    return TextRetainer(TextRetentionStrategyTail(kind="tail", max_bytes=max_bytes))


def create_text_retainer_head_tail(head_bytes: int, tail_bytes: int) -> TextRetainer:
    """Create a TextRetainer keeping head + tail."""
    return TextRetainer(
        TextRetentionStrategyHeadTail(kind="headTail", head_bytes=head_bytes, tail_bytes=tail_bytes)
    )


__all__ = [
    # Types
    "Omitted",
    "OmittedNone",
    "OmittedExact",
    "OmittedUnknown",
    "PushDecision",
    "ItemRetentionStrategy",
    "RetainedItems",
    "TextRetentionStrategy",
    "TextRetentionStrategyHead",
    "TextRetentionStrategyTail",
    "TextRetentionStrategyHeadTail",
    "RetainedText",
    "RetentionNotice",
    # Classes
    "ItemRetainer",
    "TextRetainer",
    # Formatters
    "describe_omitted",
    "format_retention_notice",
    "truncate_without_splitting_surrogate_pair",
    # Factories
    "create_item_retainer",
    "create_text_retainer_head",
    "create_text_retainer_tail",
    "create_text_retainer_head_tail",
]
