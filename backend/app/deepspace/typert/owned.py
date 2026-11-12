"""Invocation-owned values.

A resolver can return a borrowed payload together with the cleanup that
must run after the invocation settles.  :class:`OwnedValue` carries that
pair, and disposal is idempotent so a request failure, a stream close, and
an explicit cleanup cannot release the same resource twice.
"""

from __future__ import annotations

from typing import Generic, TypeVar, cast

T = TypeVar("T")


class OwnedValue(Generic[T]):
    """A borrowed payload with one idempotent release.

    The value is used through a context manager::

        with owned_value(handle, handle.close) as handle:
            await invocation.call(handle)

    Attributes:
        value: The borrowed payload.
    """

    __slots__ = ("_release", "_released", "value")

    def __init__(self, value: T, release: object) -> None:
        if not callable(release):
            raise TypeError("An owned value needs a synchronous release callable.")
        self.value = value
        self._release = release
        self._released = False

    @property
    def released(self) -> bool:
        """Whether the cleanup already ran."""
        return self._released

    def dispose(self) -> None:
        """Run the cleanup at most once."""
        if self._released:
            return
        self._released = True
        self._release()

    def __enter__(self) -> T:
        return self.value

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.dispose()

    def __repr__(self) -> str:
        return f"OwnedValue(released={self._released}, value={self.value!r})"


def owned_value(value: T, release: object) -> OwnedValue[T]:
    """Pair a borrowed value with the cleanup its owner requires.

    Args:
        value: The payload passed to the invocation.
        release: A non-throwing synchronous cleanup callable.

    Returns:
        The owned value.
    """
    return OwnedValue(value, release)


def is_owned_value(value: object) -> bool:
    """Identify an invocation-owned value across package boundaries."""
    return isinstance(value, OwnedValue)


def unwrap_owned(value: object) -> object:
    """Return the payload of an owned value, or the value unchanged."""
    if isinstance(value, OwnedValue):
        return cast(object, value.value)
    return value


__all__ = ["OwnedValue", "is_owned_value", "owned_value", "unwrap_owned"]
