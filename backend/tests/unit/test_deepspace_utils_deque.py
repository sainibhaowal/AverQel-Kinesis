"""Tests for deque utilities."""

from __future__ import annotations

import pytest

from app.deepspace.utils.deque import MIN_CAPACITY, Deque, DequeWithMaxlen


class TestDequeBasics:
    """Tests for basic Deque operations."""

    def test_empty_deque(self) -> None:
        dq = Deque[int]()
        assert len(dq) == 0
        assert dq.size == 0
        assert not dq
        assert dq.pop_front() is None
        assert dq.pop_back() is None
        assert dq.peek_front() is None
        assert dq.peek_back() is None

    def test_push_back(self) -> None:
        dq = Deque[str]()
        dq.push_back("a")
        dq.push_back("b")
        dq.push_back("c")
        assert len(dq) == 3
        assert dq.peek_front() == "a"
        assert dq.peek_back() == "c"

    def test_push_front(self) -> None:
        dq = Deque[str]()
        dq.push_front("c")
        dq.push_front("b")
        dq.push_front("a")
        assert len(dq) == 3
        assert dq.peek_front() == "a"
        assert dq.peek_back() == "c"

    def test_pop_front(self) -> None:
        dq = Deque[str]()
        dq.push_back("a")
        dq.push_back("b")
        dq.push_back("c")

        assert dq.pop_front() == "a"
        assert dq.pop_front() == "b"
        assert dq.pop_front() == "c"
        assert dq.pop_front() is None
        assert len(dq) == 0

    def test_pop_back(self) -> None:
        dq = Deque[str]()
        dq.push_back("a")
        dq.push_back("b")
        dq.push_back("c")

        assert dq.pop_back() == "c"
        assert dq.pop_back() == "b"
        assert dq.pop_back() == "a"
        assert dq.pop_back() is None
        assert len(dq) == 0

    def test_mixed_operations(self) -> None:
        dq = Deque[int]()
        dq.push_back(1)
        dq.push_front(0)
        dq.push_back(2)
        dq.push_front(-1)

        assert dq.pop_front() == -1
        assert dq.pop_front() == 0
        assert dq.pop_front() == 1
        assert dq.pop_front() == 2
        assert dq.pop_front() is None


class TestDequeIteration:
    """Tests for iteration."""

    def test_iter_forward(self) -> None:
        dq = Deque[int]()
        for i in range(5):
            dq.push_back(i)
        assert list(dq) == [0, 1, 2, 3, 4]

    def test_iter_reverse(self) -> None:
        dq = Deque[int]()
        for i in range(5):
            dq.push_back(i)
        assert list(reversed(dq)) == [4, 3, 2, 1, 0]

    def test_iter_empty(self) -> None:
        dq = Deque[int]()
        assert list(dq) == []
        assert list(reversed(dq)) == []


class TestDequeIndexing:
    """Tests for indexing."""

    def test_getitem_positive(self) -> None:
        dq = Deque[str]()
        for s in ["a", "b", "c", "d"]:
            dq.push_back(s)
        assert dq[0] == "a"
        assert dq[1] == "b"
        assert dq[2] == "c"
        assert dq[3] == "d"

    def test_getitem_negative(self) -> None:
        dq = Deque[str]()
        for s in ["a", "b", "c", "d"]:
            dq.push_back(s)
        assert dq[-1] == "d"
        assert dq[-2] == "c"
        assert dq[-3] == "b"
        assert dq[-4] == "a"

    def test_getitem_out_of_bounds(self) -> None:
        dq = Deque[str]()
        dq.push_back("a")
        with pytest.raises(IndexError):
            _ = dq[5]
        with pytest.raises(IndexError):
            _ = dq[-5]

    def test_setitem(self) -> None:
        dq = Deque[int]()
        for i in range(3):
            dq.push_back(i)
        dq[0] = 100
        dq[2] = 200
        assert dq[0] == 100
        assert dq[1] == 1
        assert dq[2] == 200

    def test_setitem_out_of_bounds(self) -> None:
        dq = Deque[int]()
        dq.push_back(1)
        with pytest.raises(IndexError):
            dq[5] = 10


class TestDequeClear:
    """Tests for clear."""

    def test_clear(self) -> None:
        dq = Deque[int]()
        for i in range(10):
            dq.push_back(i)
        dq.clear()
        assert len(dq) == 0
        assert not dq
        assert dq.pop_front() is None


class TestDequeCapacity:
    """Tests for capacity growth and compaction."""

    def test_grows_beyond_min_capacity(self) -> None:
        dq = Deque[int]()
        # Fill beyond MIN_CAPACITY (16)
        for i in range(20):
            dq.push_back(i)
        assert len(dq) == 20
        assert list(dq) == list(range(20))

    def test_compacts_after_pops(self) -> None:
        dq = Deque[int]()
        # Fill to trigger growth
        for i in range(100):
            dq.push_back(i)
        # Pop most elements
        for _ in range(95):
            dq.pop_front()
        assert len(dq) == 5
        # Should have compacted
        assert len(dq._buffer) <= 32  # Shrunk from max

    def test_does_not_shrink_below_min(self) -> None:
        dq = Deque[int]()
        for i in range(100):
            dq.push_back(i)
        for _ in range(99):
            dq.pop_front()
        assert len(dq) == 1
        assert len(dq._buffer) >= MIN_CAPACITY


class TestDequeConversion:
    """Tests for conversion to/from list."""

    def test_to_list(self) -> None:
        dq = Deque[int]()
        for i in range(5):
            dq.push_back(i)
        assert dq.to_list() == [0, 1, 2, 3, 4]

    def test_extend(self) -> None:
        dq = Deque[int]()
        dq.extend([1, 2, 3])
        assert list(dq) == [1, 2, 3]

    def test_extendleft(self) -> None:
        dq = Deque[int]()
        dq.extendleft([1, 2, 3])
        # extendleft adds in order, so last becomes front
        assert list(dq) == [3, 2, 1]


class TestDequeRotate:
    """Tests for rotate."""

    def test_rotate_right(self) -> None:
        dq = Deque[int]()
        for i in range(5):
            dq.push_back(i)
        dq.rotate(1)
        assert list(dq) == [4, 0, 1, 2, 3]
        dq.rotate(2)
        assert list(dq) == [2, 3, 4, 0, 1]

    def test_rotate_left(self) -> None:
        dq = Deque[int]()
        for i in range(5):
            dq.push_back(i)
        dq.rotate(-1)
        assert list(dq) == [1, 2, 3, 4, 0]
        dq.rotate(-2)
        assert list(dq) == [3, 4, 0, 1, 2]

    def test_rotate_full(self) -> None:
        dq = Deque[int]()
        for i in range(5):
            dq.push_back(i)
        dq.rotate(5)
        assert list(dq) == [0, 1, 2, 3, 4]
        dq.rotate(-5)
        assert list(dq) == [0, 1, 2, 3, 4]

    def test_rotate_empty(self) -> None:
        dq = Deque[int]()
        dq.rotate(10)
        assert list(dq) == []


class TestDequeRepr:
    """Tests for repr."""

    def test_repr(self) -> None:
        dq = Deque[str]()
        dq.push_back("a")
        dq.push_back("b")
        assert repr(dq) == "Deque(['a', 'b'])"

    def test_repr_empty(self) -> None:
        dq = Deque[str]()
        assert repr(dq) == "Deque([])"


class TestDequeInit:
    """Tests for initialization from iterable."""

    def test_init_from_list(self) -> None:
        dq = Deque([1, 2, 3, 4])
        assert list(dq) == [1, 2, 3, 4]

    def test_init_from_tuple(self) -> None:
        dq = Deque((1, 2, 3))
        assert list(dq) == [1, 2, 3]

    def test_init_from_generator(self) -> None:
        dq = Deque(x for x in range(3))
        assert list(dq) == [0, 1, 2]

    def test_init_empty(self) -> None:
        dq = Deque([])
        assert len(dq) == 0


class TestDequeWithMaxlen:
    """Tests for DequeWithMaxlen."""

    def test_maxlen_enforced_push_back(self) -> None:
        dq = DequeWithMaxlen[int](maxlen=3)
        dq.push_back(1)
        dq.push_back(2)
        dq.push_back(3)
        assert list(dq) == [1, 2, 3]
        dq.push_back(4)  # Should drop oldest
        assert list(dq) == [2, 3, 4]

    def test_maxlen_enforced_push_front(self) -> None:
        dq = DequeWithMaxlen[int](maxlen=3)
        dq.push_front(3)
        dq.push_front(2)
        dq.push_front(1)
        assert list(dq) == [1, 2, 3]
        dq.push_front(0)  # Should drop oldest (back)
        assert list(dq) == [0, 1, 2]

    def test_maxlen_none(self) -> None:
        dq = DequeWithMaxlen[int](maxlen=None)
        for i in range(100):
            dq.push_back(i)
        assert len(dq) == 100

    def test_maxlen_zero(self) -> None:
        dq = DequeWithMaxlen[int](maxlen=0)
        dq.push_back(1)
        assert len(dq) == 0

    def test_init_with_maxlen(self) -> None:
        dq = DequeWithMaxlen([1, 2, 3, 4, 5], maxlen=3)
        assert list(dq) == [3, 4, 5]  # Only last 3 kept


class TestDequeTypeVariations:
    """Tests with different value types."""

    def test_strings(self) -> None:
        dq = Deque[str]()
        dq.push_back("hello")
        dq.push_back("world")
        assert dq.pop_front() == "hello"

    def test_objects(self) -> None:
        dq = Deque[dict]()
        dq.push_back({"a": 1})
        dq.push_back({"b": 2})
        assert dq.pop_front() == {"a": 1}

    def test_none_values(self) -> None:
        dq = Deque[str | None]()
        dq.push_back("a")
        dq.push_back(None)
        dq.push_back("b")
        assert dq.pop_front() == "a"
        assert dq.pop_front() is None
        assert dq.pop_front() == "b"


class TestDequeConcurrency:
    """Basic concurrency sanity check (not thread-safe, but shouldn't crash)."""

    def test_rapid_operations(self) -> None:
        dq = Deque[int]()
        for i in range(1000):
            dq.push_back(i)
        for i in range(500):
            assert dq.pop_front() == i
        for i in range(500, 1000):
            dq.push_back(i)
        assert len(dq) == 1000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
