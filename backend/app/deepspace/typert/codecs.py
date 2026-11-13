"""Boundary codecs for capability parameters and results.

A codec owns three facts: whether a value has a runtime schema (``strict``)
or is only known to be source JSON (``src_json``), the type symbol a
consumer can display, and the parse/encode pair used at the boundary.

Pydantic models are the normal schema source in DeepSpace.  A plain JSON
value, a binary value, and a caller-supplied parser are the other supported
cases.
"""

from __future__ import annotations

import base64
import binascii
import enum
import inspect
import types
import typing
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Generic, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, ValidationError

from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.json_value import is_json_value

T = TypeVar("T")
ModelT = TypeVar("ModelT", bound=BaseModel)

CodecMode = Literal["strict", "src_json"]


class Codec(ABC, Generic[T]):
    """One boundary value's parse and encode contract.

    Attributes:
        mode: ``strict`` when the runtime validates a schema, ``src_json``
            when the value is only known to be JSON.
        type_symbol: The declared type's portable name.
    """

    mode: CodecMode
    type_symbol: str

    @abstractmethod
    def parse(self, value: Any) -> T:
        """Validate one untrusted boundary value."""

    def decode(self, value: Any) -> T:
        """Decode a unary result; defaults to :meth:`parse`."""
        return self.parse(value)

    def encode(self, value: T) -> Any:
        """Encode a value for the wire; defaults to the value unchanged."""
        if not is_json_value(value):
            raise RemoteError(
                "gateway/internal",
                f"The {self.type_symbol} value is not losslessly JSON-compatible.",
            )
        return value


class JsonCodec(Codec[Any]):
    """A strict codec for a value that is already known to be JSON."""

    mode: CodecMode = "strict"

    def __init__(self, type_symbol: str = "json") -> None:
        self.type_symbol = type_symbol

    def parse(self, value: Any) -> Any:
        if not is_json_value(value):
            raise RemoteError(
                "gateway/bad-request",
                f"The value for {self.type_symbol} is not losslessly JSON-compatible.",
                details={"issues": [{"path": "", "reason": "not lossless JSON"}]},
            )
        return value


class SrcJsonCodec(JsonCodec):
    """A codec for source JSON whose schema is intentionally not materialized."""

    mode: CodecMode = "src_json"

    def __init__(self, type_symbol: str) -> None:
        super().__init__(type_symbol)


class ModelCodec(Codec[ModelT], Generic[ModelT]):
    """A strict codec backed by a Pydantic model."""

    mode: CodecMode = "strict"

    def __init__(self, model: type[ModelT], *, type_symbol: str | None = None) -> None:
        if not issubclass(model, BaseModel):
            raise TypeError("ModelCodec needs a Pydantic BaseModel subclass.")
        self.model = model
        self.type_symbol = type_symbol or f"{model.__module__}.{model.__qualname__}"

    def parse(self, value: Any) -> ModelT:
        try:
            return self.model.model_validate(value)
        except ValidationError as exc:
            issues = [
                {
                    "path": ".".join(str(part) for part in item["loc"]),
                    "reason": str(item["msg"]),
                }
                for item in exc.errors(include_url=False)
            ]
            raise RemoteError(
                "gateway/bad-request",
                f"The value for {self.type_symbol} failed validation.",
                details={"issues": issues[:50]},
            ) from exc

    def encode(self, value: ModelT) -> Any:
        if not isinstance(value, BaseModel):
            raise RemoteError(
                "gateway/internal",
                f"The {self.type_symbol} encoder needs a model instance.",
            )
        return value.model_dump(mode="json")


class BinaryCodec(Codec[bytes]):
    """A strict codec that carries bytes as base64 text."""

    mode: CodecMode = "strict"
    type_symbol = "bytes"

    def parse(self, value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if not isinstance(value, str):
            raise RemoteError(
                "gateway/bad-request",
                "A binary value must be base64 text.",
            )
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise RemoteError(
                "gateway/bad-request",
                "A binary value must be valid base64 text.",
            ) from exc

    def decode(self, value: Any) -> bytes:
        return self.parse(value)

    def encode(self, value: bytes) -> str:
        if not isinstance(value, bytes):
            raise RemoteError("gateway/internal", "The binary encoder needs bytes.")
        return base64.b64encode(value).decode("ascii")


class CallableCodec(Codec[T]):
    """A strict codec backed by explicit parse and encode callables."""

    mode: CodecMode = "strict"

    def __init__(
        self,
        parser: Callable[[Any], T],
        *,
        type_symbol: str,
        encoder: Callable[[T], Any] | None = None,
    ) -> None:
        self._parser = parser
        self._encoder = encoder
        self.type_symbol = type_symbol

    def parse(self, value: Any) -> T:
        try:
            return self._parser(value)
        except RemoteError:
            raise
        except (TypeError, ValueError) as exc:
            raise RemoteError(
                "gateway/bad-request",
                f"The value for {self.type_symbol} failed validation.",
                details={"issues": [{"path": "", "reason": str(exc)}]},
            ) from exc

    def encode(self, value: T) -> Any:
        if self._encoder is None:
            return super().encode(value)
        encoded = self._encoder(value)
        if not is_json_value(encoded):
            raise RemoteError(
                "gateway/internal",
                f"The {self.type_symbol} encoder returned a non-JSON value.",
            )
        return encoded


class PrimitiveCodec(Codec[Any]):
    """A strict codec for one JSON primitive kind."""

    mode: CodecMode = "strict"

    def __init__(
        self,
        type_symbol: str,
        check: Callable[[Any], bool],
        cast_value: Callable[[Any], Any] | None = None,
    ) -> None:
        self.type_symbol = type_symbol
        self._check = check
        self._cast = cast_value or (lambda value: value)

    def parse(self, value: Any) -> Any:
        if not self._check(value):
            raise RemoteError(
                "gateway/bad-request",
                f"The value for {self.type_symbol} has the wrong type.",
                details={"issues": [{"path": "", "reason": f"expected {self.type_symbol}"}]},
            )
        return self._cast(value)

    def encode(self, value: Any) -> Any:
        if not self._check(value):
            raise RemoteError(
                "gateway/internal",
                f"The {self.type_symbol} result has the wrong type.",
            )
        return self._cast(value)


class ListCodec(Codec[list[Any]]):
    """A strict codec for a homogeneous JSON array."""

    mode: CodecMode = "strict"

    def __init__(self, item_codec: Codec[Any], *, type_symbol: str | None = None) -> None:
        self.item_codec = item_codec
        self.type_symbol = type_symbol or f"list[{item_codec.type_symbol}]"

    def parse(self, value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise RemoteError(
                "gateway/bad-request",
                f"The value for {self.type_symbol} must be an array.",
            )
        parsed: list[Any] = []
        for index, item in enumerate(value):
            try:
                parsed.append(self.item_codec.parse(item))
            except RemoteError as exc:
                raise RemoteError(
                    "gateway/bad-request",
                    f"The value for {self.type_symbol} failed validation.",
                    details={
                        "issues": [
                            {
                                "path": str(index),
                                "reason": exc.message,
                            }
                        ]
                    },
                ) from exc
        return parsed

    def encode(self, value: list[Any]) -> Any:
        if not isinstance(value, list):
            raise RemoteError(
                "gateway/internal",
                f"The {self.type_symbol} result must be a list.",
            )
        return [self.item_codec.encode(item) for item in value]


class MappingCodec(Codec[dict[str, Any]]):
    """A strict codec for a JSON object with string keys."""

    mode: CodecMode = "strict"

    def __init__(
        self,
        value_codec: Codec[Any],
        *,
        type_symbol: str | None = None,
    ) -> None:
        self.value_codec = value_codec
        self.type_symbol = type_symbol or f"dict[str, {value_codec.type_symbol}]"

    def parse(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise RemoteError(
                "gateway/bad-request",
                f"The value for {self.type_symbol} must be an object with string keys.",
            )
        parsed: dict[str, Any] = {}
        for key, item in value.items():
            try:
                parsed[key] = self.value_codec.parse(item)
            except RemoteError as exc:
                raise RemoteError(
                    "gateway/bad-request",
                    f"The value for {self.type_symbol} failed validation.",
                    details={"issues": [{"path": key, "reason": exc.message}]},
                ) from exc
        return parsed

    def encode(self, value: dict[str, Any]) -> Any:
        if not isinstance(value, dict):
            raise RemoteError(
                "gateway/internal",
                f"The {self.type_symbol} result must be an object.",
            )
        return {key: self.value_codec.encode(item) for key, item in value.items()}


class OptionalCodec(Codec[Any]):
    """A strict codec that accepts ``null`` or the wrapped value."""

    mode: CodecMode = "strict"

    def __init__(self, inner: Codec[Any]) -> None:
        self.inner = inner
        self.type_symbol = f"{inner.type_symbol} | null"

    def parse(self, value: Any) -> Any:
        if value is None:
            return None
        return self.inner.parse(value)

    def encode(self, value: Any) -> Any:
        if value is None:
            return None
        return self.inner.encode(value)


class UnionCodec(Codec[Any]):
    """A strict codec that accepts the first matching member."""

    mode: CodecMode = "strict"

    def __init__(self, codecs: Sequence[Codec[Any]], *, type_symbol: str | None = None) -> None:
        if not codecs:
            raise ValueError("A union codec needs at least one member.")
        self.codecs = tuple(codecs)
        self.type_symbol = type_symbol or " | ".join(codec.type_symbol for codec in self.codecs)

    def parse(self, value: Any) -> Any:
        failures: list[RemoteError] = []
        for codec in self.codecs:
            try:
                return codec.parse(value)
            except RemoteError as exc:
                failures.append(exc)
        raise RemoteError(
            "gateway/bad-request",
            f"The value for {self.type_symbol} matched none of its members.",
            details={"issues": [{"path": "", "reason": failure.message} for failure in failures]},
        )

    def encode(self, value: Any) -> Any:
        for codec in self.codecs:
            try:
                return codec.encode(value)
            except RemoteError:
                continue
        raise RemoteError(
            "gateway/internal",
            f"The {self.type_symbol} result matched none of its members.",
        )


class LiteralCodec(Codec[Any]):
    """A strict codec for one of a fixed set of JSON literals."""

    mode: CodecMode = "strict"

    def __init__(self, allowed: Sequence[Any], *, type_symbol: str | None = None) -> None:
        self.allowed = tuple(allowed)
        self.type_symbol = type_symbol or " | ".join(repr(item) for item in self.allowed)

    def parse(self, value: Any) -> Any:
        for item in self.allowed:
            if type(item) is type(value) and item == value:
                return value
        raise RemoteError(
            "gateway/bad-request",
            f"The value for {self.type_symbol} is not one of its allowed literals.",
        )

    def encode(self, value: Any) -> Any:
        return self.parse(value)


class EnumCodec(Codec[Any]):
    """A strict codec for one Python ``Enum`` member."""

    mode: CodecMode = "strict"

    def __init__(self, enum_type: type[enum.Enum]) -> None:
        self.enum_type = enum_type
        self.type_symbol = f"{enum_type.__module__}.{enum_type.__qualname__}"

    def parse(self, value: Any) -> Any:
        try:
            return self.enum_type(value)
        except (ValueError, TypeError) as exc:
            raise RemoteError(
                "gateway/bad-request",
                f"The value for {self.type_symbol} is not a declared member.",
            ) from exc

    def encode(self, value: Any) -> Any:
        if not isinstance(value, self.enum_type):
            raise RemoteError(
                "gateway/internal",
                f"The {self.type_symbol} result is not a member.",
            )
        return value.value


def codec_for(annotation: Any, *, type_symbol: str | None = None) -> Codec[Any]:
    """Choose the strict codec for a Python annotation.

    ``str``, ``int``, ``float``, ``bool``, arrays, mappings, unions,
    optionals, literals, enums, Pydantic models, and ``bytes`` all get a
    validating codec.  ``Any`` and an unknown annotation get the JSON codec,
    which still checks that the value survives JSON losslessly.

    Args:
        annotation: The declared parameter or result type.
        type_symbol: An optional portable name that overrides the inferred
            one.

    Returns:
        The boundary codec.
    """
    if annotation is inspect.Signature.empty or annotation is Any or annotation is None:
        return JsonCodec(type_symbol or "json")
    if isinstance(annotation, str):
        return _codec_for_text(annotation, type_symbol=type_symbol)
    if annotation is bytes or annotation is bytearray:
        return BinaryCodec()
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return ModelCodec(annotation, type_symbol=type_symbol)
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return EnumCodec(annotation)

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {typing.Union, types.UnionType}:
        none_type = type(None)
        members = [item for item in arguments if item is not none_type]
        optional = len(members) != len(arguments)
        member_codecs = [codec_for(item) for item in members]
        inner: Codec[Any] = (
            member_codecs[0]
            if len(member_codecs) == 1
            else UnionCodec(member_codecs, type_symbol=type_symbol)
        )
        return OptionalCodec(inner) if optional else inner
    if origin in {list, Sequence} or annotation is list:
        item = arguments[0] if arguments else Any
        return ListCodec(codec_for(item), type_symbol=type_symbol)
    if origin in {dict, Mapping} or annotation is dict:
        value_type = arguments[1] if len(arguments) == 2 else Any
        key_type = arguments[0] if len(arguments) == 2 else str
        if key_type not in {str, Any}:
            return JsonCodec(type_symbol or _annotation_name(annotation))
        return MappingCodec(codec_for(value_type), type_symbol=type_symbol)
    if origin is Literal:
        return LiteralCodec(arguments, type_symbol=type_symbol)

    if annotation is str:
        return PrimitiveCodec("string", lambda value: isinstance(value, str))
    if annotation is bool:
        return PrimitiveCodec("boolean", lambda value: isinstance(value, bool))
    if annotation is int:
        return PrimitiveCodec(
            "integer",
            lambda value: isinstance(value, int) and not isinstance(value, bool),
        )
    if annotation is float:
        return PrimitiveCodec(
            "number",
            lambda value: isinstance(value, int | float) and not isinstance(value, bool),
            lambda value: float(value),
        )
    return JsonCodec(type_symbol or _annotation_name(annotation))


def _codec_for_text(annotation: str, *, type_symbol: str | None) -> Codec[Any]:
    normalized = annotation.strip().strip("'\"")
    if normalized in {"bytes", "bytearray"}:
        return BinaryCodec()
    simple: dict[str, Codec[Any]] = {
        "str": PrimitiveCodec("string", lambda value: isinstance(value, str)),
        "bool": PrimitiveCodec("boolean", lambda value: isinstance(value, bool)),
        "int": PrimitiveCodec(
            "integer",
            lambda value: isinstance(value, int) and not isinstance(value, bool),
        ),
        "float": PrimitiveCodec(
            "number",
            lambda value: isinstance(value, int | float) and not isinstance(value, bool),
            lambda value: float(value),
        ),
        "Any": JsonCodec(type_symbol or "json"),
        "list": ListCodec(JsonCodec(), type_symbol=type_symbol),
        "dict": MappingCodec(JsonCodec(), type_symbol=type_symbol),
    }
    if normalized in simple:
        return simple[normalized]
    return JsonCodec(type_symbol or normalized)


def _annotation_name(annotation: Any) -> str:
    if annotation is Any or annotation is None:
        return "json"
    name = getattr(annotation, "__name__", None)
    if isinstance(name, str):
        return name
    return str(annotation).replace("typing.", "")


__all__ = [
    "BinaryCodec",
    "CallableCodec",
    "Codec",
    "CodecMode",
    "EnumCodec",
    "JsonCodec",
    "ListCodec",
    "LiteralCodec",
    "MappingCodec",
    "ModelCodec",
    "OptionalCodec",
    "PrimitiveCodec",
    "SrcJsonCodec",
    "UnionCodec",
    "codec_for",
]
