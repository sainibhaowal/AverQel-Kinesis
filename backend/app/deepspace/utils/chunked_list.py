"""Persistent append-only chunked lists for DeepSpace.

Provides immutable chunked lists with structural sharing, bounded copying
(64 items per chunk), and JSON checkpoint validation. Matches the semantics
of @deepseek-ai/dsh-chunked-list.

Usage:
    from app.deepspace.utils.chunked_list import ChunkedList, append_chunked_list, iterate_chunked_list

    # Empty list
    lst: ChunkedList[str] | None = None

    # Append (creates new list sharing unchanged chunks)
    lst = append_chunked_list(lst, "item1")
    lst = append_chunked_list(lst, "item2")

    # Iterate in insertion order
    for item in iterate_chunked_list(lst):
        print(item)

    # Validate checkpoint with Pydantic
    from app.deepspace.utils.chunked_list import chunked_list_schema
    schema = chunked_list_schema(str)
    validated = schema.validate_python(lst)
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

T = TypeVar("T")

CHUNK_CAPACITY = 64


@dataclass(frozen=True)
class ChunkedList(Generic[T]):
    """A persistent append-only chunked list node.

    Each node holds up to CHUNK_CAPACITY values and a reference to the
    previous node. Newer chunks are at the head; iteration yields values
    in insertion order (oldest first).
    """

    values: tuple[T, ...]
    previous: ChunkedList[T] | None = None

    def __post_init__(self) -> None:
        if len(self.values) == 0:
            raise ValueError("ChunkedList chunk cannot be empty")
        if len(self.values) > CHUNK_CAPACITY:
            raise ValueError(f"ChunkedList chunk exceeds capacity of {CHUNK_CAPACITY}")

    def __len__(self) -> int:
        """Total number of values in the list."""
        count = len(self.values)
        if self.previous:
            count += len(self.previous)
        return count

    def __bool__(self) -> bool:
        return True

    def __iter__(self) -> Iterator[T]:
        """Iterate values in insertion order."""
        return iterate_chunked_list(self)

    def __getitem__(self, index: int) -> T:
        """Get value by index (O(N/64) time). Supports negative indexing."""
        # Collect all chunks to get total length
        chunks: list[ChunkedList[T]] = []
        current: ChunkedList[T] | None = self
        while current:
            chunks.append(current)
            current = current.previous
        total_len = sum(len(c.values) for c in chunks)

        if index < 0:
            index += total_len
        if index < 0 or index >= total_len:
            raise IndexError("Index out of range")

        # Reverse to get oldest first
        for chunk in reversed(chunks):
            if index < len(chunk.values):
                return chunk.values[index]
            index -= len(chunk.values)
        raise IndexError("Index out of range")


def append_chunked_list(head: ChunkedList[T] | None, value: T) -> ChunkedList[T]:
    """Append a value without modifying the input, copying at most one chunk.

    Args:
        head: Current list head, or None for empty list.
        value: Value to append.

    Returns:
        New list head sharing unchanged older chunks.
    """
    if head is None or len(head.values) >= CHUNK_CAPACITY:
        # New chunk
        return ChunkedList(values=(value,), previous=head)
    # Append to existing chunk
    return ChunkedList(values=(*head.values, value), previous=head.previous)


def prepend_chunked_list(head: ChunkedList[T] | None, value: T) -> ChunkedList[T]:
    """Prepend a value (adds to front, not typically used for persistent lists)."""
    # A prepend inserts before the first value in the current oldest chunk.
    # Rebuild the chain in order so the result remains a valid persistent list.
    result = from_list([value, *iterate_chunked_list(head)])
    assert result is not None
    return result


def iterate_chunked_list(head: ChunkedList[T] | None) -> Iterator[T]:
    """Visit all values in insertion order (oldest first).

    Time: O(N), Space: O(N/64) for chunk collection.
    """
    if head is None:
        return

    # Collect chunks from newest to oldest
    chunks: list[ChunkedList[T]] = []
    current: ChunkedList[T] | None = head
    while current is not None:
        chunks.append(current)
        current = current.previous

    # Yield from oldest to newest
    for chunk in reversed(chunks):
        yield from chunk.values


def to_list(head: ChunkedList[T] | None) -> list[T]:
    """Convert to a regular list (copies all values)."""
    if head is None:
        return []
    return list(iterate_chunked_list(head))


def from_list(values: list[T]) -> ChunkedList[T] | None:
    """Build a chunked list from a regular list."""
    head: ChunkedList[T] | None = None
    for v in values:
        head = append_chunked_list(head, v)
    return head


def chunked_list_len(head: ChunkedList[T] | None) -> int:
    """Get total length of the list."""
    if head is None:
        return 0
    return len(head)


def chunked_list_is_empty(head: ChunkedList[T] | None) -> bool:
    """Check if list is empty."""
    return head is None


def chunked_list_get(head: ChunkedList[T] | None, index: int) -> T | None:
    """Get value by index, or None if out of bounds."""
    if head is None:
        return None
    try:
        return head[index]
    except IndexError:
        return None


# Pydantic schema for checkpoint validation
def chunked_list_schema(value_schema: Any) -> type[BaseModel]:
    """Create a Pydantic schema for validating ChunkedList checkpoints.

    Args:
        value_schema: Pydantic type or schema for each stored value.

    Returns:
        A Pydantic model class that validates ChunkedList structure.

    Example:
        from pydantic import BaseModel
        schema = chunked_list_schema(str)
        validated = schema.model_validate({"values": ["a", "b"], "previous": {"values": ["c"]}})
    """
    # Use a recursive model so loaded checkpoints keep typed ``previous``
    # nodes, and reject misspelled fields instead of silently discarding them.
    model_name = (
        f"ChunkedList_{value_schema.__name__}"
        if hasattr(value_schema, "__name__")
        else "ChunkedList_Value"
    )
    dynamic_chunk = create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", arbitrary_types_allowed=True),
        values=(list[value_schema], Field(min_length=1, max_length=CHUNK_CAPACITY)),
        previous=(f"{model_name} | None", None),
    )
    dynamic_chunk.model_rebuild(_types_namespace={model_name: dynamic_chunk})

    return cast(type[Any], dynamic_chunk)


# Alias for backward compatibility
ChunkedListSchema = chunked_list_schema


def validate_chunked_list(
    head: ChunkedList[T] | None, value_type: type[T]
) -> ChunkedList[T] | None:
    """Validate a chunked list at runtime (for checkpoint loading).

    Args:
        head: The list head to validate.
        value_type: Expected type of values.

    Returns:
        The validated head, or raises ValidationError.
    """
    if head is None:
        return None

    # Convert to dict for validation
    def to_dict(node: ChunkedList[T] | None) -> dict[str, Any] | None:
        if node is None:
            return None
        return {
            "values": list(node.values),
            "previous": to_dict(node.previous),
        }

    schema = chunked_list_schema(value_type)
    validated = schema.model_validate(to_dict(head))

    # Convert back to ChunkedList
    def from_dict(node: Any | None) -> ChunkedList[T] | None:
        if node is None:
            return None
        return ChunkedList(
            values=tuple(node.values),
            previous=from_dict(node.previous) if node.previous else None,
        )

    return from_dict(validated)


__all__ = [
    "CHUNK_CAPACITY",
    "ChunkedList",
    "append_chunked_list",
    "prepend_chunked_list",
    "iterate_chunked_list",
    "to_list",
    "from_list",
    "chunked_list_len",
    "chunked_list_is_empty",
    "chunked_list_get",
    "chunked_list_schema",
    "ChunkedListSchema",
    "validate_chunked_list",
]
