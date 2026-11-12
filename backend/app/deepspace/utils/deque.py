"""Circular deque with auto-compact for DeepSpace.

Provides a zero-dependency circular deque with amortized constant-time
end operations and bounded vacant storage. Shrinks backing storage when
live entry count reaches 1/4 of capacity. Matches the semantics of
@deepseek-ai/dsh-deque.

Usage:
    from app.deepspace.utils.deque import Deque

    dq = Deque[str]()
    dq.push_back("a")
    dq.push_back("b")
    dq.push_front("first")

    while dq:
        print(dq.pop_front())
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Generic, TypeVar

T = TypeVar("T")

MIN_CAPACITY = 16


class Deque(Generic[T]):
    """A circular deque with amortized O(1) insertion and removal.

    Removed entries are cleared immediately, and sparse storage shrinks
    after the live entry count reaches one quarter of its capacity.

    Unlike collections.deque, this implementation:
    - Explicitly clears removed entries (helps GC)
    - Auto-compacts storage when sparse
    - Provides typed interface with Generics
    """

    __slots__ = ("_buffer", "_head", "_count")

    def __init__(self, iterable: Iterable[T] | None = None) -> None:
        """Initialize the deque.

        Args:
            iterable: Optional initial items.
        """
        self._buffer: list[T | None] = [None] * MIN_CAPACITY
        self._head = 0
        self._count = 0

        if iterable:
            for item in iterable:
                self.push_back(item)

    @property
    def size(self) -> int:
        """Number of entries available to remove."""
        return self._count

    def __len__(self) -> int:
        return self._count

    def __bool__(self) -> bool:
        return self._count > 0

    def __iter__(self) -> Iterator[T]:
        """Iterate from front to back."""
        for i in range(self._count):
            idx = self._head + i
            if idx >= len(self._buffer):
                idx -= len(self._buffer)
            yield self._buffer[idx]  # type: ignore[misc]

    def __reversed__(self) -> Iterator[T]:
        """Iterate from back to front."""
        for i in range(self._count - 1, -1, -1):
            idx = self._head + i
            if idx >= len(self._buffer):
                idx -= len(self._buffer)
            yield self._buffer[idx]  # type: ignore[misc]

    def __getitem__(self, index: int) -> T:
        """Get item by index (0 = front). Supports negative indexing."""
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError("Deque index out of range")
        idx = self._head + index
        if idx >= len(self._buffer):
            idx -= len(self._buffer)
        value = self._buffer[idx]
        if value is None:
            raise IndexError("Deque index out of range (cleared)")
        return value

    def __setitem__(self, index: int, value: T) -> None:
        """Set item by index."""
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError("Deque index out of range")
        idx = self._head + index
        if idx >= len(self._buffer):
            idx -= len(self._buffer)
        self._buffer[idx] = value

    def __repr__(self) -> str:
        items = ", ".join(repr(x) for x in self)
        return f"Deque([{items}])"

    def push_back(self, value: T) -> None:
        """Append one entry after the current tail.

        Args:
            value: Entry to append.
        """
        self._ensure_capacity()
        tail = self._head + self._count
        if tail >= len(self._buffer):
            tail -= len(self._buffer)
        self._buffer[tail] = value
        self._count += 1

    def push_front(self, value: T) -> None:
        """Insert one entry before the current head.

        Args:
            value: Entry to prepend.
        """
        self._ensure_capacity()
        self._head = self._head - 1 if self._head > 0 else len(self._buffer) - 1
        self._buffer[self._head] = value
        self._count += 1

    def pop_front(self) -> T | None:
        """Remove and return the current head entry.

        Clears the retained reference immediately.

        Returns:
            The removed entry, or None if deque is empty.
        """
        if self._count == 0:
            return None
        value = self._buffer[self._head]
        self._buffer[self._head] = None
        self._head += 1
        if self._head == len(self._buffer):
            self._head = 0
        self._count -= 1
        self._compact()
        return value

    def pop_back(self) -> T | None:
        """Remove and return the current tail entry.

        Clears the retained reference immediately.

        Returns:
            The removed entry, or None if deque is empty.
        """
        if self._count == 0:
            return None
        tail = self._head + self._count - 1
        if tail >= len(self._buffer):
            tail -= len(self._buffer)
        value = self._buffer[tail]
        self._buffer[tail] = None
        self._count -= 1
        self._compact()
        return value

    def peek_front(self) -> T | None:
        """Return the current head entry without removing it."""
        if self._count == 0:
            return None
        return self._buffer[self._head]

    def peek_back(self) -> T | None:
        """Return the current tail entry without removing it."""
        if self._count == 0:
            return None
        tail = self._head + self._count - 1
        if tail >= len(self._buffer):
            tail -= len(self._buffer)
        return self._buffer[tail]

    def clear(self) -> None:
        """Drop every entry and release the current backing storage."""
        self._buffer = [None] * MIN_CAPACITY
        self._head = 0
        self._count = 0

    def to_list(self) -> list[T]:
        """Convert to a regular list (copies all values)."""
        return list(self)

    def extend(self, iterable: Iterable[T]) -> None:
        """Extend the deque by appending elements from iterable."""
        for item in iterable:
            self.push_back(item)

    def extendleft(self, iterable: Iterable[T]) -> None:
        """Extend the deque by prepending elements from iterable.

        Note: Elements are added in order, so the last element of
        iterable becomes the new front.
        """
        for item in iterable:
            self.push_front(item)

    def rotate(self, n: int = 1) -> None:
        """Rotate the deque n steps to the right (negative = left)."""
        if self._count == 0:
            return
        n = n % self._count
        if n == 0:
            return
        if n > 0:
            # Rotate right: move tail items to front
            for _ in range(n):
                val = self.pop_back()
                if val is not None:
                    self.push_front(val)
        else:
            # Rotate left: move front items to back
            for _ in range(-n):
                val = self.pop_front()
                if val is not None:
                    self.push_back(val)

    def _ensure_capacity(self) -> None:
        """Grow buffer if at capacity."""
        if self._count < len(self._buffer):
            return
        self._resize(len(self._buffer) * 2)

    def _compact(self) -> None:
        """Shrink buffer if sparse (count <= capacity/4)."""
        if self._count == 0:
            self._head = 0
            return
        if len(self._buffer) > MIN_CAPACITY and self._count <= len(self._buffer) // 4:
            self._resize(max(MIN_CAPACITY, len(self._buffer) // 2))

    def _resize(self, capacity: int) -> None:
        """Resize buffer to new capacity, preserving order."""
        new_buffer: list[T | None] = [None] * capacity
        source = self._head
        for i in range(self._count):
            new_buffer[i] = self._buffer[source]
            source += 1
            if source == len(self._buffer):
                source = 0
        self._buffer = new_buffer
        self._head = 0


# Alias for compatibility with collections.deque usage patterns
class DequeWithMaxlen(Deque[T]):
    """Deque variant with maximum length (drops oldest when full)."""

    def __init__(self, iterable: Iterable[T] | None = None, maxlen: int | None = None) -> None:
        if maxlen is not None and maxlen < 0:
            raise ValueError("maxlen must be non-negative")
        self._maxlen = maxlen
        super().__init__(iterable)

    @property
    def maxlen(self) -> int | None:
        return self._maxlen

    def push_back(self, value: T) -> None:
        if self._maxlen == 0:
            return
        if self._maxlen is not None and self._count >= self._maxlen:
            self.pop_front()
        super().push_back(value)

    def push_front(self, value: T) -> None:
        if self._maxlen == 0:
            return
        if self._maxlen is not None and self._count >= self._maxlen:
            self.pop_back()
        super().push_front(value)


__all__ = [
    "Deque",
    "DequeWithMaxlen",
    "MIN_CAPACITY",
]
