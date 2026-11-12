"""Tests for chunked_list utilities."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from app.deepspace.utils.chunked_list import (
    CHUNK_CAPACITY,
    ChunkedList,
    append_chunked_list,
    chunked_list_get,
    chunked_list_is_empty,
    chunked_list_len,
    chunked_list_schema,
    from_list,
    iterate_chunked_list,
    prepend_chunked_list,
    to_list,
    validate_chunked_list,
)


class TestChunkedListBasics:
    """Tests for ChunkedList basic operations."""

    def test_empty_list(self) -> None:
        """Test empty list (None)."""
        assert chunked_list_is_empty(None)
        assert chunked_list_len(None) == 0
        assert chunked_list_get(None, 0) is None
        assert list(iterate_chunked_list(None)) == []

    def test_single_append(self) -> None:
        """Test appending single item."""
        head = append_chunked_list(None, "a")
        assert not chunked_list_is_empty(head)
        assert chunked_list_len(head) == 1
        assert chunked_list_get(head, 0) == "a"
        assert list(iterate_chunked_list(head)) == ["a"]

    def test_multiple_appends_same_chunk(self) -> None:
        """Test multiple appends within one chunk."""
        head: ChunkedList[str] | None = None
        for i in range(10):
            head = append_chunked_list(head, f"item{i}")
        assert chunked_list_len(head) == 10
        assert list(iterate_chunked_list(head)) == [f"item{i}" for i in range(10)]

    def test_chunk_boundary(self) -> None:
        """Test appending across chunk boundary (64 items)."""
        head: ChunkedList[int] | None = None
        for i in range(CHUNK_CAPACITY + 5):
            head = append_chunked_list(head, i)
        assert chunked_list_len(head) == CHUNK_CAPACITY + 5
        assert list(iterate_chunked_list(head)) == list(range(CHUNK_CAPACITY + 5))

    def test_many_chunks(self) -> None:
        """Test many chunks (3+ chunks)."""
        head: ChunkedList[int] | None = None
        total = CHUNK_CAPACITY * 3 + 10
        for i in range(total):
            head = append_chunked_list(head, i)
        assert chunked_list_len(head) == total
        assert list(iterate_chunked_list(head)) == list(range(total))

    def test_structural_sharing(self) -> None:
        """Test that older chunks are shared."""
        head1 = append_chunked_list(None, "a")
        head1 = append_chunked_list(head1, "b")

        head2 = append_chunked_list(head1, "c")

        # head1 and head2 should share the first chunk
        assert head1.previous is head2.previous
        assert head1.values == ("a", "b")
        assert head2.values == ("a", "b", "c")


class TestChunkedListIteration:
    """Tests for iteration."""

    def test_iteration_order(self) -> None:
        """Test iteration yields in insertion order."""
        head: ChunkedList[int] | None = None
        for i in range(5):
            head = append_chunked_list(head, i)
        assert list(iterate_chunked_list(head)) == [0, 1, 2, 3, 4]

    def test_iteration_empty(self) -> None:
        """Test iteration of empty list."""
        assert list(iterate_chunked_list(None)) == []

    def test_iteration_cross_chunk(self) -> None:
        """Test iteration across chunk boundaries."""
        head: ChunkedList[int] | None = None
        for i in range(CHUNK_CAPACITY + 3):
            head = append_chunked_list(head, i)
        result = list(iterate_chunked_list(head))
        assert result == list(range(CHUNK_CAPACITY + 3))

    def test_reversed_iteration(self) -> None:
        """Test reversed iteration."""
        head: ChunkedList[int] | None = None
        for i in range(5):
            head = append_chunked_list(head, i)
        assert list(reversed(head)) == [4, 3, 2, 1, 0]


class TestChunkedListIndexing:
    """Tests for indexing operations."""

    def test_get_item(self) -> None:
        """Test __getitem__."""
        head: ChunkedList[str] | None = None
        for s in ["a", "b", "c"]:
            head = append_chunked_list(head, s)
        assert head[0] == "a"
        assert head[1] == "b"
        assert head[2] == "c"

    def test_get_item_negative(self) -> None:
        """Test negative indexing."""
        head: ChunkedList[str] | None = None
        for s in ["a", "b", "c"]:
            head = append_chunked_list(head, s)
        assert head[-1] == "c"
        assert head[-2] == "b"
        assert head[-3] == "a"

    def test_get_item_out_of_bounds(self) -> None:
        """Test out of bounds raises IndexError."""
        head: ChunkedList[str] | None = None
        head = append_chunked_list(head, "a")
        with pytest.raises(IndexError):
            _ = head[5]
        with pytest.raises(IndexError):
            _ = head[-5]

    def test_chunked_list_get_helper(self) -> None:
        """Test chunked_list_get returns None for OOB."""
        head: ChunkedList[str] | None = None
        head = append_chunked_list(head, "a")
        assert chunked_list_get(head, 0) == "a"
        assert chunked_list_get(head, 5) is None
        assert chunked_list_get(None, 0) is None


class TestChunkedListConversion:
    """Tests for to_list and from_list."""

    def test_to_list(self) -> None:
        """Test conversion to regular list."""
        head: ChunkedList[int] | None = None
        for i in range(5):
            head = append_chunked_list(head, i)
        assert to_list(head) == [0, 1, 2, 3, 4]

    def test_to_list_empty(self) -> None:
        assert to_list(None) == []

    def test_from_list(self) -> None:
        """Test building from regular list."""
        head = from_list([1, 2, 3, 4, 5])
        assert chunked_list_len(head) == 5
        assert list(iterate_chunked_list(head)) == [1, 2, 3, 4, 5]

    def test_from_list_empty(self) -> None:
        assert from_list([]) is None

    def test_round_trip(self) -> None:
        """Test round-trip conversion."""
        original = list(range(100))
        head = from_list(original)
        assert to_list(head) == original


class TestChunkedListPrepend:
    """Tests for prepend (less common)."""

    def test_prepend(self) -> None:
        head = prepend_chunked_list(None, "first")
        head = prepend_chunked_list(head, "second")
        # Each new value is inserted at the front.
        assert list(iterate_chunked_list(head)) == ["second", "first"]


class TestChunkedListLen:
    """Tests for len function."""

    def test_len_empty(self) -> None:
        assert chunked_list_len(None) == 0

    def test_len_single_chunk(self) -> None:
        head: ChunkedList[int] | None = None
        for i in range(10):
            head = append_chunked_list(head, i)
        assert chunked_list_len(head) == 10

    def test_len_multi_chunk(self) -> None:
        head: ChunkedList[int] | None = None
        for i in range(CHUNK_CAPACITY * 2 + 5):
            head = append_chunked_list(head, i)
        assert chunked_list_len(head) == CHUNK_CAPACITY * 2 + 5


class TestChunkedListSchema:
    """Tests for Pydantic schema validation."""

    def test_chunked_list_schema_str(self) -> None:
        """Test schema for string values."""
        schema = chunked_list_schema(str)

        # Valid single chunk
        data = {"values": ["a", "b"], "previous": None}
        validated = schema.model_validate(data)
        assert validated.values == ["a", "b"]
        assert validated.previous is None

    def test_chunked_list_schema_nested(self) -> None:
        """Test schema with nested previous."""
        schema = chunked_list_schema(str)

        data = {
            "values": ["c", "d"],
            "previous": {
                "values": ["a", "b"],
                "previous": None,
            },
        }
        validated = schema.model_validate(data)
        assert validated.values == ["c", "d"]
        assert validated.previous is not None
        assert validated.previous.values == ["a", "b"]

    def test_chunked_list_schema_rejects_empty(self) -> None:
        """Test schema rejects empty chunk."""
        schema = chunked_list_schema(str)
        with pytest.raises(ValidationError):
            schema.model_validate({"values": [], "previous": None})

    def test_chunked_list_schema_rejects_oversized(self) -> None:
        """Test schema rejects oversized chunk."""
        schema = chunked_list_schema(str)
        with pytest.raises(ValidationError):
            schema.model_validate({"values": ["x"] * (CHUNK_CAPACITY + 1), "previous": None})

    def test_chunked_list_schema_rejects_unknown_fields(self) -> None:
        """Test schema rejects unknown fields."""
        schema = chunked_list_schema(str)
        with pytest.raises(ValidationError):
            schema.model_validate({"values": ["a"], "unknown": "field"})

    def test_validate_chunked_list(self) -> None:
        """Test validate_chunked_list function."""
        head: ChunkedList[str] | None = None
        for s in ["a", "b", "c"]:
            head = append_chunked_list(head, s)

        validated = validate_chunked_list(head, str)
        assert validated is not None
        assert list(iterate_chunked_list(validated)) == ["a", "b", "c"]

    def test_validate_chunked_list_empty(self) -> None:
        assert validate_chunked_list(None, str) is None


class TestChunkedListDataclass:
    """Tests for ChunkedList dataclass features."""

    def test_frozen(self) -> None:
        head = ChunkedList(values=("a",))
        with pytest.raises(FrozenInstanceError):
            head.values = ("b",)  # type: ignore[misc]

    def test_slots(self) -> None:
        head = ChunkedList(values=("a",))
        with pytest.raises(AttributeError):
            head.new_attr = "value"  # type: ignore[misc]

    def test_post_init_validates_empty(self) -> None:
        with pytest.raises(ValueError):
            ChunkedList(values=())

    def test_post_init_validates_capacity(self) -> None:
        with pytest.raises(ValueError):
            ChunkedList(values=tuple(range(CHUNK_CAPACITY + 1)))

    def test_bool_true(self) -> None:
        head = ChunkedList(values=("a",))
        assert bool(head) is True

    def test_repr(self) -> None:
        head = ChunkedList(values=("a", "b"))
        assert "ChunkedList" in repr(head)


class TestChunkedListEdgeCases:
    """Tests for edge cases."""

    def test_large_list_performance(self) -> None:
        """Test that large lists work efficiently."""
        head: ChunkedList[int] | None = None
        for i in range(10000):
            head = append_chunked_list(head, i)
        assert chunked_list_len(head) == 10000
        # Sample some values
        assert head[0] == 0
        assert head[5000] == 5000
        assert head[9999] == 9999

    def test_different_types(self) -> None:
        """Test with different value types."""
        # Integers
        head_int: ChunkedList[int] | None = None
        for i in range(5):
            head_int = append_chunked_list(head_int, i)
        assert list(iterate_chunked_list(head_int)) == [0, 1, 2, 3, 4]

        # Floats
        head_float: ChunkedList[float] | None = None
        for f in [1.1, 2.2, 3.3]:
            head_float = append_chunked_list(head_float, f)
        assert list(iterate_chunked_list(head_float)) == [1.1, 2.2, 3.3]

        # Objects
        head_obj: ChunkedList[dict] | None = None
        for d in [{"a": 1}, {"b": 2}]:
            head_obj = append_chunked_list(head_obj, d)
        assert list(iterate_chunked_list(head_obj)) == [{"a": 1}, {"b": 2}]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
