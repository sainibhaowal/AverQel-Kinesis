"""
Value utilities for DeepSpace.

Provides JSON validation, structural equality, deep freeze, and weak map with values.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast
from weakref import WeakKeyDictionary

# ============================================================================
# Types
# ============================================================================

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


# ============================================================================
# JSON Validation & Snapshotting
# ============================================================================


def _has_plain_object_prototype(value: object) -> bool:
    """Check if an object is a plain dict (not a class instance)."""
    return type(value) is dict


def _has_plain_array_prototype(value: list[Any]) -> bool:
    """Check if an array uses the intrinsic Array.prototype."""
    return list in type(value).__mro__


def _is_lossless_json_number(value: float) -> bool:
    """Test a float that survives JSON without a value change.

    ``NaN`` and the infinities are not JSON values.  Negative zero is
    representable but JavaScript's ``JSON.parse`` loses its sign, so the
    guard rejects it just like the Harness boundary does.
    """
    return math.isfinite(value) and not (value == 0.0 and math.copysign(1.0, value) < 0.0)


def _enumerable_string_keys(value: object) -> list[str] | None:
    """Get all enumerable string keys, or None if non-enumerable/non-string keys exist."""
    try:
        keys = []
        for key in dir(value):
            if not key.startswith("_") and isinstance(getattr(value, key), property):
                continue
        # For dict-like objects
        if hasattr(value, "keys"):
            for item_key in value.keys():
                if not isinstance(item_key, str):
                    return None
                if not hasattr(value, "__dict__") or item_key in value.__dict__:
                    keys.append(item_key)
            return keys
        return None
    except Exception:
        return None


# ============================================================================
# JSON Validation & Snapshotting
# ============================================================================


def _walk_json_value(value: Any, detach: bool) -> JsonValue | bool | None:
    """
    Iteratively validate lossless JSON, optionally materializing a detached snapshot.
    Returns the snapshot if detach=True, True if valid, None if invalid.
    """
    frames: list[dict[str, Any]] = []
    root: Any = None

    def assign(item: Any) -> None:
        nonlocal root
        if not frames:
            if detach:
                root = item
        else:
            frame = frames[-1]
            if frame["kind"] == "array":
                if frame["target"] is not None:
                    # For arrays, use append since we process elements sequentially
                    frame["target"].append(item)
            elif frame["target"] is not None:
                frame["target"][frame["key"]] = item

    # Push root frame
    if isinstance(value, list):
        if not _has_plain_array_prototype(value):
            return None
        length = len(value)
        if len([k for k in range(len(value)) if not isinstance(k, int)]) != 0:
            return None
        target: list[Any] | dict[str, Any] | None = [] if detach else None
        frames.append(
            {
                "kind": "array",
                "source": value,
                "length": len(value),
                "index": 0,
                "target": target,
            }
        )
    elif isinstance(value, dict):
        if not _has_plain_object_prototype(value):
            return None
        keys = list(value.keys())
        for key in value.keys():
            if not isinstance(key, str):
                return None
        if any(not isinstance(k, str) for k in keys):
            return None
        target = {} if detach else None
        frames.append(
            {
                "kind": "object",
                "source": value,
                "keys": list(value.keys()),
                "index": 0,
                "key": "",
                "target": target,
            }
        )
    elif value is None or isinstance(value, bool | str | int):
        return value if detach else True
    elif isinstance(value, float):
        if not _is_lossless_json_number(value):
            if detach:
                return None
            return False
        return value if detach else True
    else:
        # Non-JSON type at root
        if detach:
            return None
        return False

    ancestors: set[int] = set()
    if isinstance(value, dict | list):
        ancestors.add(id(value))

    current: Any = None

    while frames:
        frame = frames[-1]
        length = frame["length"] if frame["kind"] == "array" else len(frame["keys"])

        if frame["index"] >= length:
            # Frame exhausted, pop and assign completed value to parent
            if frame["kind"] == "object" or frame["kind"] == "array":
                ancestors.discard(id(frame["source"]))
            frames.pop()
            if not frames:
                # This was the root frame
                if detach:
                    return cast(JsonValue, frame["target"])
                return True
            # Assign completed value to parent frame
            parent_frame = frames[-1]
            if parent_frame["target"] is not None:
                if parent_frame["kind"] == "array":
                    parent_frame["target"].append(frame["target"])
                else:
                    # Use parent frame's key (the key that referenced this child)
                    parent_frame["target"][parent_frame["key"]] = frame["target"]
            continue

        # Get next child
        if frame["kind"] == "array":
            key = frame["index"]
            frame["index"] += 1
            if not hasattr(frame["source"], "__getitem__") or key >= len(frame["source"]):
                return None
            current = frame["source"][key]
            frame["key"] = key
        else:
            keys = frame["keys"]
            key = keys[frame["index"]]
            frame["index"] += 1
            frame["key"] = key
            if key not in frame["source"]:
                return None
            current = frame["source"][key]

        # Process current value
        if current is None or isinstance(current, bool | str | int):
            # Primitive value, assign to current frame (which is the parent frame)
            if not frames:
                return None
            frame = frames[-1]
            if frame["target"] is not None:
                if frame["kind"] == "array":
                    frame["target"].append(current)
                else:
                    frame["target"][frame["key"]] = current
            continue  # Get next sibling
        if isinstance(current, float):
            if not _is_lossless_json_number(current):
                return None
            frame = frames[-1]
            if frame["target"] is not None:
                if frame["kind"] == "array":
                    frame["target"].append(current)
                else:
                    frame["target"][frame["key"]] = current
            continue  # Get next sibling
        if isinstance(current, dict | list):
            # Check for cycles
            if id(current) in ancestors:
                return None

            if isinstance(current, list):
                if not _has_plain_array_prototype(current):
                    return None
                length = len(current)
                if len([k for k in range(len(current)) if not isinstance(k, int)]) != 0:
                    return None
                target = [] if detach else None
                frames.append(
                    {
                        "kind": "array",
                        "source": current,
                        "length": length,
                        "index": 0,
                        "target": target,
                    }
                )
                ancestors.add(id(current))
                continue  # Process children in next iteration
            else:
                if not _has_plain_object_prototype(current):
                    return None
                keys = list(current.keys())
                for key in current.keys():
                    if not isinstance(key, str):
                        return None
                if any(not isinstance(k, str) for k in keys):
                    return None
                target = {} if detach else None
                frames.append(
                    {
                        "kind": "object",
                        "source": current,
                        "keys": keys,
                        "index": 0,
                        "key": "",
                        "target": target,
                    }
                )
                ancestors.add(id(current))
                continue  # Process children in next iteration

        # A value that is neither JSON primitive nor a plain dict/list.
        return None

    return root if detach else True


def snapshot_json_value(value: Any) -> JsonValue | None:
    """
    Validate and detach lossless JSON in one read per property.

    Args:
        value: Candidate value to validate and detach.

    Returns:
        The detached snapshot, or None when the value is not losslessly
        JSON-serializable.
    """
    return cast(JsonValue | None, _walk_json_value(value, True))


def is_json_value(value: Any) -> bool:
    """
    Test the same lossless JSON rules as snapshot_json_value without detaching.

    Args:
        value: Candidate value to test.

    Returns:
        Whether the value survives a JSON round trip without loss.
    """
    return _walk_json_value(value, False) is True


def deep_equal_json(a: Any, b: Any) -> bool:
    """
    Compare JSON-compatible values structurally.

    Args:
        a: First JSON-compatible value.
        b: Second JSON-compatible value.

    Returns:
        Whether both values contain the same JSON data.
    """
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if a is None or b is None:
        return a is b
    if isinstance(a, bool | int | float | str):
        return bool(a == b)
    if isinstance(a, list):
        if not isinstance(b, list) or len(a) != len(b):
            return False
        return all(deep_equal_json(x, y) for x, y in zip(a, b, strict=False))
    if isinstance(a, dict):
        if not isinstance(b, dict) or len(a) != len(b):
            return False
        if set(a.keys()) != set(b.keys()):
            return False
        return all(deep_equal_json(a[k], b[k]) for k in a.keys())
    return False


def deep_freeze(value: Any) -> Any:
    """
    Deep-freeze an object graph in place while leaving live AbortSignal objects mutable.

    Args:
        value: Value to freeze.

    Returns:
        The same value after every reachable enumerable child is frozen.
    """
    seen = set()
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if node is None or not isinstance(node, dict | list | object):
            continue
        if hasattr(node, "__dict__") and isinstance(getattr(node, "__dict__", None), dict):
            # Check if it's an asyncio Event or similar
            if type(node).__name__ in ("Event", "Task", "Future", "TimerHandle"):
                continue
        if id(node) in seen:
            continue
        seen.add(id(node))

        try:
            # Freeze the object
            if isinstance(node, dict):
                # Convert to MappingProxyType for immutability
                frozen: Any = type(node)(node)
                frozen.__frozen__ = True
            elif isinstance(node, list):
                frozen = tuple(node)
            else:
                continue

            # Add children to stack
            if isinstance(node, dict):
                for v in node.values():
                    stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)
        except (TypeError, AttributeError):
            # Some objects can't be frozen
            pass

    return value


# ============================================================================
# WeakMap with Value Retention
# ============================================================================

K = TypeVar("K")
V = TypeVar("V")


class WeakMapWithValues(Generic[K, V]):
    """
    Weak-key lookup with a strongly retained iterable set of associated values.

    Each value must belong to only one key. The container performs no automatic
    cleanup; owners delete associations or clear the container at lifecycle end.
    """

    def __init__(self) -> None:
        self._keys: WeakKeyDictionary[K, V] = WeakKeyDictionary()
        self._value_set: set[V] = set()

    @property
    def values(self) -> set[V]:
        """Live strongly retained values in insertion order."""
        return self._value_set

    def get(self, key: K) -> V | None:
        """Read the value associated with a key."""
        return self._keys.get(key)

    def has(self, key: K) -> bool:
        """Test whether a key has an association."""
        return key in self._keys

    def set(self, key: K, value: V) -> WeakMapWithValues[K, V]:
        """
        Associate one key with one caller-unique value.

        Args:
            key: Weakly held lookup key.
            value: Strongly retained value that belongs to no other key.

        Returns:
            This container for chaining.
        """
        if key in self._keys:
            previous = self._keys[key]
            if previous is value:
                return self
            self._value_set.discard(previous)
        self._keys[key] = value
        self._value_set.add(value)
        return self

    def delete(self, key: K) -> bool:
        """
        Remove one association and its strongly retained value.

        Args:
            key: Weakly held lookup key.

        Returns:
            Whether an association was removed.
        """
        if key not in self._keys:
            return False
        value = self._keys[key]
        del self._keys[key]
        self._value_set.discard(value)
        return True

    def clear(self) -> None:
        """Remove every association and strongly retained value."""
        self._keys = WeakKeyDictionary()
        self._value_set.clear()

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key: K) -> bool:
        return key in self._keys

    def __iter__(self) -> Iterator[tuple[K, V]]:
        yield from self._keys.items()


# ============================================================================
# Partial JSON Parsing (for streaming)
# ============================================================================


@dataclass(frozen=True, slots=True)
class PartialArguments:
    """Partial JSON arguments with length limit options."""

    max_length: int = 10_000
    max_depth: int = 100


LengthReadOptions = PartialArguments


def parse_partial_json(
    text: str,
    options: LengthReadOptions | None = None,
) -> dict[str, Any]:
    """
    Parse JSON with length and depth limits for safety.

    Args:
        text: JSON text to parse.
        options: Length and depth limits.

    Returns:
        Parsed JSON object.

    Raises:
        ValueError: If limits exceeded or invalid JSON.
    """
    options = options or LengthReadOptions()

    if len(text) > options.max_length:
        raise ValueError(f"JSON length {len(text)} exceeds maximum {options.max_length}")

    # Track depth during parsing
    depth = 0
    max_depth = options.max_depth

    def check_depth(char: str) -> None:
        nonlocal depth
        if char in "{[":
            depth += 1
            if depth > max_depth:
                raise ValueError(f"JSON depth {depth} exceeds maximum {max_depth}")
        elif char in "}]":
            depth -= 1

    for char in text:
        check_depth(char)

    return cast(dict[str, Any], json.loads(text))


# ============================================================================
# Assertions
# ============================================================================


def assert_never(value: Any, context: str = "") -> None:
    """
    Mark an unreachable closed-union branch.

    Args:
        value: Impossible value; an unhandled typed variant fails at the call site.
        context: Optional switch-site label included in the failure message.

    Raises:
        AssertionError: Always, with the rendered value.
    """
    rendered = json.dumps(value) if value is not None else str(value)
    raise AssertionError(f"unreachable variant{context and f' in {context}'}: {rendered}")


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    # Types
    "JsonValue",
    # Validation
    "snapshot_json_value",
    "is_json_value",
    "deep_equal_json",
    "deep_freeze",
    "assert_never",
    # WeakMap
    "WeakMapWithValues",
    # Partial JSON
    "PartialArguments",
    "LengthReadOptions",
    "parse_partial_json",
]
