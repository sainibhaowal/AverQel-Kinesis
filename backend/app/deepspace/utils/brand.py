"""Primitive-backed branded types for DeepSpace identifiers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

T = TypeVar("T", str, int, float, uuid.UUID, bytes)


@dataclass(frozen=True, slots=True, eq=False)
class Branded(Generic[T]):
    """Immutable wrapper used for values without a JSON primitive subtype."""

    value: T

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return self.value == (other.value if isinstance(other, Branded) else other)

    def __hash__(self) -> int:
        return hash(self.value)

    def __int__(self) -> int:
        return int(self.value)

    def __float__(self) -> float:
        return float(self.value)

    def __bytes__(self) -> bytes:
        return bytes(self.value)

    def __len__(self) -> int:
        return len(self.value)  # type: ignore[arg-type]

    def __bool__(self) -> bool:
        return bool(self.value)


class BrandedStr(str):
    """String subtype that stays JSON-serializable as an ordinary string."""

    __slots__ = ()
    _brand_name: str | None = None

    @property
    def value(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str.__repr__(self)})"


class BrandedInt(int):
    """Integer subtype that stays JSON-serializable as an ordinary integer."""

    __slots__ = ()
    _brand_name: str | None = None

    @property
    def value(self) -> int:
        return int(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({int.__repr__(self)})"


class BrandedFloat(float):
    """Float subtype that stays JSON-serializable as an ordinary number."""

    __slots__ = ()
    _brand_name: str | None = None

    @property
    def value(self) -> float:
        return float(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({float.__repr__(self)})"


class BrandedBytes(bytes):
    """Bytes subtype with a primitive-compatible immutable value."""

    __slots__ = ()
    _brand_name: str | None = None

    @property
    def value(self) -> bytes:
        return bytes(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({bytes.__repr__(self)})"


@dataclass(frozen=True, slots=True, eq=False)
class BrandedUUID(Branded[uuid.UUID]):
    """UUID wrapper retaining normal UUID comparison and string conversion."""

    _brand_name: ClassVar[str | None] = None


_PRIMITIVE_BRANDS: dict[type[Any], type[Any]] = {
    str: BrandedStr,
    int: BrandedInt,
    float: BrandedFloat,
    bytes: BrandedBytes,
    uuid.UUID: BrandedUUID,
}


def branded(name: str, base_type: type[T]) -> type[Any]:
    """Create an immutable branded subtype for a supported primitive type."""
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError("brand name must be a valid Python identifier")
    base = _PRIMITIVE_BRANDS.get(base_type)
    if base is None:
        raise TypeError(f"unsupported branded primitive: {base_type!r}")
    return type(
        name,
        (base,),
        {
            "__module__": __name__,
            "__doc__": f"Branded {base_type.__name__} for {name}.",
            "__slots__": (),
            "_brand_name": name,
        },
    )


def brand_string(value: str, brand_name: str) -> BrandedStr:
    """Return a JSON-compatible branded string value."""
    return branded(brand_name, str)(value)


def brand_int(value: int, brand_name: str) -> BrandedInt:
    """Return a JSON-compatible branded integer value."""
    return branded(brand_name, int)(value)


def brand_uuid(value: uuid.UUID, brand_name: str) -> BrandedUUID:
    """Return a branded UUID value."""
    return branded(brand_name, uuid.UUID)(value)


# Common identifiers use the same primitive representation as their source.
SessionId = branded("SessionId", str)
ConversationId = branded("ConversationId", str)
AgentId = branded("AgentId", str)
ToolCallId = branded("ToolCallId", str)
RunId = branded("RunId", str)
JobId = branded("JobId", str)
TenantId = branded("TenantId", str)
UserId = branded("UserId", str)
WorkspaceId = branded("WorkspaceId", str)
ModelId = branded("ModelId", str)
ProviderId = branded("ProviderId", str)
SecretId = branded("SecretId", str)
FileId = branded("FileId", str)
AttachmentId = branded("AttachmentId", str)
VectorId = branded("VectorId", str)
EmbeddingId = branded("EmbeddingId", str)
RerankerId = branded("RerankerId", str)
CollectionId = branded("CollectionId", str)
DocumentId = branded("DocumentId", str)
ChunkId = branded("ChunkId", str)
EventId = branded("EventId", str)
LogId = branded("LogId", str)
TraceId = branded("TraceId", str)
SpanId = branded("SpanId", str)
RequestId = branded("RequestId", str)
ResponseId = branded("ResponseId", str)
MessageId = branded("MessageId", str)
ThreadId = branded("ThreadId", str)
ProcessId = branded("ProcessId", int)
PortNumber = branded("PortNumber", int)
TimeoutMs = branded("TimeoutMs", int)
TokenCount = branded("TokenCount", int)
ByteCount = branded("ByteCount", int)
Score = branded("Score", float)
Probability = branded("Probability", float)
Latitude = branded("Latitude", float)
Longitude = branded("Longitude", float)

# Type-only aliases for code that wants to group brands by primitive type.
StrId = BrandedStr
IntId = BrandedInt
UUIDId = BrandedUUID


def is_branded(value: Any, brand_name: str) -> bool:
    """Check whether a value belongs to the named branded subtype."""
    return getattr(type(value), "_brand_name", None) == brand_name


def get_brand_name(value: Any) -> str:
    """Return the brand's declared name, falling back to the runtime class."""
    return str(getattr(type(value), "_brand_name", None) or type(value).__name__)


def unbrand(value: Any) -> Any:
    """Return the underlying primitive value."""
    return value.value if hasattr(value, "value") else value


__all__ = [
    "Branded",
    "BrandedStr",
    "BrandedInt",
    "BrandedFloat",
    "BrandedUUID",
    "BrandedBytes",
    "StrId",
    "IntId",
    "UUIDId",
    "branded",
    "brand_string",
    "brand_int",
    "brand_uuid",
    "is_branded",
    "get_brand_name",
    "unbrand",
    "SessionId",
    "ConversationId",
    "AgentId",
    "ToolCallId",
    "RunId",
    "JobId",
    "TenantId",
    "UserId",
    "WorkspaceId",
    "ModelId",
    "ProviderId",
    "SecretId",
    "FileId",
    "AttachmentId",
    "VectorId",
    "EmbeddingId",
    "RerankerId",
    "CollectionId",
    "DocumentId",
    "ChunkId",
    "EventId",
    "LogId",
    "TraceId",
    "SpanId",
    "RequestId",
    "ResponseId",
    "MessageId",
    "ThreadId",
    "ProcessId",
    "PortNumber",
    "TimeoutMs",
    "TokenCount",
    "ByteCount",
    "Score",
    "Probability",
    "Latitude",
    "Longitude",
    "StrId",
    "IntId",
    "UUIDId",
]
