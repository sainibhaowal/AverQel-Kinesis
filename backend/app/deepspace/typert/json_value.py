"""Lossless JSON checks shared by every capability carrier.

The rules are the same as :mod:`app.deepspace.utils.values`: plain objects,
plain arrays, string keys, no cycles, and finite numbers that keep their
sign.  This module adds the uplink rule: a top-level "undefined" value may
travel as a frame without a value, while nested undefined values may not.

Python has no ``undefined``; :data:`UNSET` is the explicit sentinel a
caller uses when it needs that top-level distinction.  ``None`` is the JSON
``null`` value and follows the ordinary JSON rules.
"""

from __future__ import annotations

from typing import Any

from app.deepspace.typert.errors import RemoteError
from app.deepspace.utils.values import is_json_value as _is_json_value
from app.deepspace.utils.values import snapshot_json_value as _snapshot_json_value


class _Unset:
    """The explicit top-level "no value" sentinel."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


#: A top-level value that travels as an uplink frame without a value.
UNSET = _Unset()


def is_json_value(value: Any) -> bool:
    """Test whether a value crosses JSON without coercion or omission."""
    return _is_json_value(value)


def snapshot_json_value(value: Any) -> Any:
    """Validate and detach a lossless JSON value.

    Raises:
        RemoteError: With ``gateway/bad-request`` when the value is not
            losslessly JSON-compatible.
    """
    if not _is_json_value(value):
        raise RemoteError(
            "gateway/bad-request",
            "The value is not losslessly JSON-compatible.",
            details={"issues": [{"path": "", "reason": "not lossless JSON"}]},
        )
    return _snapshot_json_value(value)


def is_uplink_item(value: Any) -> bool:
    """Test whether a value may travel as one uplink item."""
    return value is UNSET or _is_json_value(value)


def snapshot_uplink_item(value: Any) -> Any:
    """Return a detached uplink value, preserving :data:`UNSET`.

    Raises:
        RemoteError: With ``gateway/bad-request`` when the value is not
            losslessly JSON-compatible.
    """
    if value is UNSET:
        return UNSET
    return snapshot_json_value(value)


__all__ = [
    "UNSET",
    "is_json_value",
    "is_uplink_item",
    "snapshot_json_value",
    "snapshot_uplink_item",
]
