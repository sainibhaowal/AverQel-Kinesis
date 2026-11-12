"""The typed failure vocabulary of DeepSpace's native capability protocol.

Every capability call crosses one failure vocabulary: a stable code, a
human-readable message, and an optional details object.  The base codes are
declared here; a package can merge its own codes with
:func:`register_error_code` next to the code that might raise it.

The vocabulary exists so a producer, a transport, and a consumer agree on a
failure without parsing an exception string.  It is the Python-native
counterpart of the Harness ``RemoteError``/``RemoteErrorDetailsMap`` pair.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, cast

#: The base failure codes every deployment understands.
_BASE_CODES: dict[str, str] = {
    "gateway/bad-request": "The owner refused the request as invalid.",
    "gateway/cancelled": "The call was cancelled by the caller or the backend.",
    "gateway/internal": "An unclassified host failure stopped the call.",
    "gateway/unauthorized": "The caller is not authorized for the call.",
    "gateway/forbidden": "The caller is authorized but not allowed for this resource.",
    "gateway/not-found": "The requested capability or resource does not exist.",
    "gateway/conflict": "The call conflicts with the current state.",
    "gateway/unavailable": "The owner is temporarily unavailable.",
    "validation/failed": "A boundary value failed validation.",
    "resource/denied": "Access to one declared resource was denied.",
    "stream/closed": "The stream is closed or was disposed.",
    "stream/backpressure": "The uplink window is full.",
}


@dataclass(frozen=True, slots=True)
class RemoteErrorDetails:
    """One failure that crossed a capability boundary.

    Attributes:
        code: The stable failure code.
        message: An actionable message for the caller.
        details: Small JSON-compatible context, such as validation issues.
    """

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible wire form of this failure."""
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class RemoteError(Exception):
    """A typed failure raised by a capability implementation.

    A caller catches :class:`RemoteError`, reads ``code`` and ``details``,
    and returns a stable result instead of depending on an exception type
    that belongs to the implementation.
    """

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not code or code not in _BASE_CODES:
            raise ValueError(f"Unknown remote error code: {code!r}")
        self.code = code
        self.message = message or _BASE_CODES[code]
        self.details = dict(details or {})
        super().__init__(self.message)

    def as_details(self) -> RemoteErrorDetails:
        """Return this exception as the frozen failure value."""
        return RemoteErrorDetails(
            code=self.code,
            message=self.message,
            details=dict(self.details),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible wire form of this failure."""
        return self.as_details().as_dict()


def register_error_code(code: str, description: str) -> None:
    """Declare one package-owned failure code.

    The registration is idempotent for the same description.  A different
    description for an existing code is refused, so two owners cannot
    silently disagree about what a code means.

    Args:
        code: The stable code, such as ``workspace/path-required``.
        description: What the failure means.

    Raises:
        ValueError: If the code is empty or already declared differently.
    """
    if not code or "/" not in code:
        raise ValueError("A remote error code needs a namespace and a name.")
    existing = _BASE_CODES.get(code)
    if existing is not None and existing != description:
        raise ValueError(f"Remote error code {code!r} is already declared.")
    _BASE_CODES[code] = description


def known_error_codes() -> Mapping[str, str]:
    """Return a read-only view of every declared failure code."""
    return dict(_BASE_CODES)


def is_remote_error(value: object) -> bool:
    """Identify a failure by its structural marker, not its class identity.

    This mirrors the Harness cross-realm marker: a failure produced by a
    separately loaded package is still recognizable after it crosses a
    process boundary.
    """
    if isinstance(value, RemoteError):
        return True
    if isinstance(value, Mapping):
        code = value.get("code")
        return isinstance(code, str) and code in _BASE_CODES and "message" in value
    return False


def remote_error_of(error: BaseException) -> RemoteError:
    """Normalize any exception into the shared failure vocabulary.

    A :class:`RemoteError` is returned unchanged.  A ``ValueError`` becomes
    ``gateway/bad-request``; any other exception becomes
    ``gateway/internal``.  The original exception text is preserved in the
    message so a caller can still act on it.
    """
    if isinstance(error, RemoteError):
        return error
    if isinstance(error, ValueError):
        return RemoteError("gateway/bad-request", str(error) or None)
    return RemoteError("gateway/internal", str(error) or None)


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RemoteResult(Generic[T]):
    """A success value or a typed failure, never both.

    Attributes:
        ok: Whether the call succeeded.
        value: The decoded result when ``ok`` is true.
        error: The failure when ``ok`` is false.
    """

    ok: bool
    value: T | None = None
    error: RemoteErrorDetails | None = None

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("A successful result cannot carry an error.")
        if not self.ok and self.error is None:
            raise ValueError("A failed result must carry an error.")

    @classmethod
    def success(cls, value: T) -> RemoteResult[T]:
        """Build a successful result."""
        return cls(ok=True, value=value)

    @classmethod
    def failure(
        cls,
        error: RemoteError | RemoteErrorDetails,
    ) -> RemoteResult[T]:
        """Build a failed result from a typed failure."""
        details = error.as_details() if isinstance(error, RemoteError) else error
        return cls(ok=False, error=details)

    def unwrap(self) -> T:
        """Return the value or raise the typed failure.

        Raises:
            RemoteError: When the result is a failure.
        """
        if not self.ok:
            assert self.error is not None
            raise RemoteError(
                self.error.code,
                self.error.message,
                details=self.error.details,
            )
        return cast(T, self.value)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible wire form of this result."""
        if self.ok:
            return {"ok": True, "value": self.value}
        assert self.error is not None
        return {"ok": False, "error": self.error.as_dict()}


__all__ = [
    "RemoteError",
    "RemoteErrorDetails",
    "RemoteResult",
    "is_remote_error",
    "known_error_codes",
    "register_error_code",
    "remote_error_of",
]
