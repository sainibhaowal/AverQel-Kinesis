"""Primitive-backed branded types for DeepSpace identifiers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Generic, NewType, TypeVar, cast

T = TypeVar("T", str, int, float, uuid.UUID, bytes)

# Type-checking-only proper NewType definitions for mypy
if TYPE_CHECKING:
    SessionId = NewType("SessionId", str)
    ConversationId = NewType("ConversationId", str)
    AgentId = NewType("AgentId", str)
    ToolCallId = NewType("ToolCallId", str)
    RunId = NewType("RunId", str)
    JobId = NewType("JobId", str)
    TenantId = NewType("TenantId", str)
    UserId = NewType("UserId", str)
    WorkspaceId = NewType("WorkspaceId", str)
    ModelId = NewType("ModelId", str)
    ProviderId = NewType("ProviderId", str)
    SecretId = NewType("SecretId", str)
    FileId = NewType("FileId", str)
    AttachmentId = NewType("AttachmentId", str)
    VectorId = NewType("VectorId", str)
    EmbeddingId = NewType("EmbeddingId", str)
    RerankerId = NewType("RerankerId", str)
    CollectionId = NewType("CollectionId", str)
    DocumentId = NewType("DocumentId", str)
    ChunkId = NewType("ChunkId", str)
    EventId = NewType("EventId", str)
    LogId = NewType("LogId", str)
    TraceId = NewType("TraceId", str)
    SpanId = NewType("SpanId", str)
    RequestId = NewType("RequestId", str)
    ResponseId = NewType("ResponseId", str)
    MessageId = NewType("MessageId", str)
    ThreadId = NewType("ThreadId", str)
    ProcessId = NewType("ProcessId", int)
    PortNumber = NewType("PortNumber", int)
    TimeoutMs = NewType("TimeoutMs", int)
    TokenCount = NewType("TokenCount", int)
    ByteCount = NewType("ByteCount", int)
    Score = NewType("Score", float)
    Probability = NewType("Probability", float)
    Latitude = NewType("Latitude", float)
    Longitude = NewType("Longitude", float)


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
        return float(cast(Any, self.value))

    def __bytes__(self) -> bytes:
        return bytes(cast(Any, self.value))

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
    return cast(BrandedStr, branded(brand_name, str)(value))


def brand_int(value: int, brand_name: str) -> BrandedInt:
    """Return a JSON-compatible branded integer value."""
    return cast(BrandedInt, branded(brand_name, int)(value))


def brand_uuid(value: uuid.UUID, brand_name: str) -> BrandedUUID:
    """Return a branded UUID value."""
    return cast(BrandedUUID, branded(brand_name, uuid.UUID)(value))


# Common identifiers use the same primitive representation as their source.
SessionId = branded("SessionId", str)  # type: ignore[misc]
ConversationId = branded("ConversationId", str)  # type: ignore[misc]
AgentId = branded("AgentId", str)  # type: ignore[misc]
ToolCallId = branded("ToolCallId", str)  # type: ignore[misc]
RunId = branded("RunId", str)  # type: ignore[misc]
JobId = branded("JobId", str)  # type: ignore[misc]
TenantId = branded("TenantId", str)  # type: ignore[misc]
UserId = branded("UserId", str)  # type: ignore[misc]
WorkspaceId = branded("WorkspaceId", str)  # type: ignore[misc]
ModelId = branded("ModelId", str)  # type: ignore[misc]
ProviderId = branded("ProviderId", str)  # type: ignore[misc]
SecretId = branded("SecretId", str)  # type: ignore[misc]
FileId = branded("FileId", str)  # type: ignore[misc]
AttachmentId = branded("AttachmentId", str)  # type: ignore[misc]
VectorId = branded("VectorId", str)  # type: ignore[misc]
EmbeddingId = branded("EmbeddingId", str)  # type: ignore[misc]
RerankerId = branded("RerankerId", str)  # type: ignore[misc]
CollectionId = branded("CollectionId", str)  # type: ignore[misc]
DocumentId = branded("DocumentId", str)  # type: ignore[misc]
ChunkId = branded("ChunkId", str)  # type: ignore[misc]
EventId = branded("EventId", str)  # type: ignore[misc]
LogId = branded("LogId", str)  # type: ignore[misc]
TraceId = branded("TraceId", str)  # type: ignore[misc]
SpanId = branded("SpanId", str)  # type: ignore[misc]
RequestId = branded("RequestId", str)  # type: ignore[misc]
ResponseId = branded("ResponseId", str)  # type: ignore[misc]
MessageId = branded("MessageId", str)  # type: ignore[misc]
ThreadId = branded("ThreadId", str)  # type: ignore[misc]
ProcessId = branded("ProcessId", int)  # type: ignore[misc]
PortNumber = branded("PortNumber", int)  # type: ignore[misc]
TimeoutMs = branded("TimeoutMs", int)  # type: ignore[misc]
TokenCount = branded("TokenCount", int)  # type: ignore[misc]
ByteCount = branded("ByteCount", int)  # type: ignore[misc]
Score = branded("Score", float)  # type: ignore[misc]
Probability = branded("Probability", float)  # type: ignore[misc]
Latitude = branded("Latitude", float)  # type: ignore[misc]
Longitude = branded("Longitude", float)  # type: ignore[misc]

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
