"""The declaration of one invocable capability.

A descriptor is the complete wire contract of one method: its stable
endpoint, the receiver, the ordered parameters, the codecs, the optional
uplink, the cancellation parameter, the declared resources, and the result
codec.  A registry validates the whole descriptor before it becomes
visible, so a malformed contract cannot be half-registered.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.deepspace.typert.codecs import Codec, JsonCodec
from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.resources import ResourceAccess, dedupe_resources

RemoteMode = Literal["unary", "stream"]
ReceiverKind = Literal["direct", "context"]
ParameterSource = Literal["json", "lookup"]

_ENDPOINT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")


@dataclass(frozen=True, slots=True)
class RemoteParameter:
    """One ordered parameter of a capability call.

    Attributes:
        name: The parameter name in the implementation signature.
        codec: The boundary codec.
        source: ``json`` for a wire value, ``lookup`` for a resolved host
            object.
        accepts_undefined: Whether a top-level missing value is allowed.
        wire: The wire field for a lookup parameter.
        resource_kind: The declared resource kind a lookup identity names.
    """

    name: str
    codec: Codec[Any] = field(default_factory=JsonCodec)
    source: ParameterSource = "json"
    accepts_undefined: bool = False
    wire: str | None = None
    resource_kind: str | None = None
    lookup: str | None = None

    def validate(self) -> None:
        """Validate this parameter's own declaration."""
        if not self.name or not self.name.isidentifier():
            raise RemoteError(
                "gateway/internal",
                f"Capability parameter name {self.name!r} is not a Python identifier.",
            )
        if self.source not in {"json", "lookup"}:
            raise RemoteError(
                "gateway/internal",
                f"Capability parameter {self.name!r} has an unknown source.",
            )
        if self.source == "lookup":
            if not self.wire:
                raise RemoteError(
                    "gateway/internal",
                    f"Lookup parameter {self.name!r} needs a wire field.",
                )
            if not self.lookup:
                raise RemoteError(
                    "gateway/internal",
                    f"Lookup parameter {self.name!r} needs a lookup key.",
                )
            if self.accepts_undefined:
                raise RemoteError(
                    "gateway/internal",
                    f"Lookup parameter {self.name!r} cannot accept undefined.",
                )
        elif self.lookup is not None:
            raise RemoteError(
                "gateway/internal",
                f"JSON parameter {self.name!r} cannot declare a lookup key.",
            )
        if self.resource_kind == "global":
            raise RemoteError(
                "gateway/internal",
                "The global resource has no lookup identity.",
            )


@dataclass(frozen=True, slots=True)
class RemoteReceiver:
    """How a method receives its owning context.

    Attributes:
        kind: ``direct`` for a plain method, ``context`` when the method
            receives a resolved context object.
        wire: The wire field of the context identity.
        codec: The codec of the context identity.
    """

    kind: ReceiverKind = "direct"
    wire: str | None = None
    codec: Codec[Any] | None = None
    context: str | None = None

    def validate(self) -> None:
        """Validate the receiver declaration."""
        if self.kind not in {"direct", "context"}:
            raise RemoteError("gateway/internal", "Unknown capability receiver kind.")
        if self.kind == "context" and (not self.wire or self.codec is None or not self.context):
            raise RemoteError(
                "gateway/internal",
                "A context receiver needs a wire field, a Context key, and a codec.",
            )


@dataclass(frozen=True, slots=True)
class RemoteDescriptor:
    """The complete wire declaration of one capability method."""

    id: str
    service: str
    namespace: str
    method: str
    implementation: Callable[..., Any] | None
    mode: RemoteMode = "unary"
    receiver: RemoteReceiver = field(default_factory=RemoteReceiver)
    parameters: tuple[RemoteParameter, ...] = ()
    uplink_codec: Codec[Any] | None = None
    cancellation_parameter: str | None = None
    result_codec: Codec[Any] = field(default_factory=JsonCodec)
    scope: str | None = None
    description: str | None = None
    resources: tuple[ResourceAccess, ...] = ()
    source: str | None = None
    invocable: bool = True

    @property
    def endpoint(self) -> str:
        """The stable endpoint id."""
        return self.id

    def validate(self) -> None:
        """Validate the complete declaration.

        Raises:
            RemoteError: With ``gateway/internal`` for a declaration bug.
        """
        if not self.id or not _ENDPOINT_PATTERN.match(self.id):
            raise RemoteError(
                "gateway/internal",
                f"Capability endpoint {self.id!r} is empty or malformed.",
            )
        if not self.service or not self.method or not self.method.isidentifier():
            raise RemoteError(
                "gateway/internal",
                f"Capability {self.id!r} needs a service and a method.",
            )
        if self.mode not in {"unary", "stream"}:
            raise RemoteError("gateway/internal", f"Capability {self.id!r} has an unknown mode.")
        if self.mode == "stream" and self.uplink_codec is None:
            raise RemoteError(
                "gateway/internal",
                f"Streaming capability {self.id!r} needs an uplink codec.",
            )
        if self.invocable and self.implementation is None:
            raise RemoteError(
                "gateway/internal",
                f"Invocable capability {self.id!r} needs an implementation.",
            )
        if self.cancellation_parameter is not None and self.cancellation_parameter != "signal":
            raise RemoteError(
                "gateway/internal",
                "The cancellation parameter must be named 'signal'.",
            )
        names: set[str] = set()
        for parameter in self.parameters:
            parameter.validate()
            if parameter.name in names:
                raise RemoteError(
                    "gateway/internal",
                    f"Capability {self.id!r} repeats parameter {parameter.name!r}.",
                )
            names.add(parameter.name)
        if self.cancellation_parameter is not None and self.cancellation_parameter in names:
            raise RemoteError(
                "gateway/internal",
                "The cancellation signal is supplied separately from the parameters.",
            )
        self.receiver.validate()
        for access in dedupe_resources(self.resources):
            if access.kind == "global":
                continue
            if not access.id:
                raise RemoteError(
                    "gateway/internal",
                    f"Capability {self.id!r} declares an empty resource identity.",
                )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible, inspectable descriptor."""
        return {
            "id": self.id,
            "service": self.service,
            "namespace": self.namespace,
            "method": self.method,
            "mode": self.mode,
            "receiver": self.receiver.kind,
            "receiver_wire": self.receiver.wire,
            "receiver_context": self.receiver.context,
            "parameters": [
                {
                    "name": parameter.name,
                    "source": parameter.source,
                    "type_symbol": parameter.codec.type_symbol,
                    "accepts_undefined": parameter.accepts_undefined,
                    "wire": parameter.wire,
                    "resource_kind": parameter.resource_kind,
                    "lookup": parameter.lookup,
                }
                for parameter in self.parameters
            ],
            "uplink_type_symbol": (
                self.uplink_codec.type_symbol if self.uplink_codec is not None else None
            ),
            "cancellation_parameter": self.cancellation_parameter,
            "result_type_symbol": self.result_codec.type_symbol,
            "scope": self.scope,
            "description": self.description,
            "resources": [access.as_dict() for access in self.resources],
            "source": self.source,
            "invocable": self.invocable,
        }


@dataclass(frozen=True, slots=True)
class RemoteContribution:
    """One package's atomic set of schemas and descriptors.

    The registry validates the entire contribution before it publishes
    anything, and its disposer withdraws exactly this contribution.
    """

    package: str
    face: str = "host"
    schemas: tuple[Any, ...] = ()
    descriptors: tuple[RemoteDescriptor, ...] = ()
    package_model: Any | None = None
    version: int = 1

    def validate(self) -> None:
        """Validate the contribution's package identity."""
        if not self.package or not self.face:
            raise RemoteError(
                "gateway/internal",
                "A capability contribution needs a package and a face.",
            )
        if self.version < 1:
            raise RemoteError(
                "gateway/internal",
                "A capability contribution version must be positive.",
            )
        seen: set[str] = set()
        for descriptor in self.descriptors:
            descriptor.validate()
            if descriptor.id in seen:
                raise RemoteError(
                    "gateway/conflict",
                    f"Contribution {self.package!r} repeats endpoint {descriptor.id!r}.",
                )
            seen.add(descriptor.id)


@dataclass(frozen=True, slots=True)
class RemoteMethodOptions:
    """Decorator options collected before binding a service."""

    namespace: str | None = None
    name: str | None = None
    mode: RemoteMode = "unary"
    parameters: tuple[RemoteParameter, ...] | None = None
    uplink_codec: Codec[Any] | None = None
    cancellation_parameter: str | None = None
    result_codec: Codec[Any] | None = None
    scope: str | None = None
    resources: tuple[ResourceAccess, ...] = ()
    description: str | None = None


def descriptor_for_method(
    *,
    service_key: str,
    method_name: str,
    implementation: Callable[..., Any],
    options: RemoteMethodOptions,
    default_parameters: Sequence[RemoteParameter],
) -> RemoteDescriptor:
    """Build a descriptor from a decorated method and its options."""
    namespace = options.namespace or service_key
    method = options.name or method_name
    return RemoteDescriptor(
        id=f"{namespace}/{method}",
        service=service_key,
        namespace=namespace,
        method=method,
        implementation=implementation,
        mode=options.mode,
        parameters=(
            tuple(options.parameters)
            if options.parameters is not None
            else tuple(default_parameters)
        ),
        uplink_codec=options.uplink_codec,
        cancellation_parameter=options.cancellation_parameter,
        result_codec=options.result_codec or JsonCodec(),
        scope=options.scope,
        description=options.description or implementation.__doc__,
        resources=options.resources,
        source=f"{implementation.__module__}.{implementation.__qualname__}",
    )


__all__ = [
    "ParameterSource",
    "ReceiverKind",
    "RemoteContribution",
    "RemoteDescriptor",
    "RemoteMethodOptions",
    "RemoteMode",
    "RemoteParameter",
    "RemoteReceiver",
    "descriptor_for_method",
]
