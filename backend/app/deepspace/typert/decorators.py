"""Mark Python methods as capability endpoints and bind a service.

The decorators are the Python-native counterpart of the Harness
``@Remote``/``@RemoteScope`` pair.  A decorated method carries an immutable
option object; :func:`bind_remote` turns every decorated method on one
service instance into a validated descriptor.

A static method, a class method, a property, and a private method are
refused: each of them either has no owner at call time or is not part of a
service's public capability surface.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar, cast, get_type_hints

from app.deepspace.typert.codecs import Codec, JsonCodec, codec_for
from app.deepspace.typert.descriptors import (
    RemoteDescriptor,
    RemoteMethodOptions,
    RemoteParameter,
)
from app.deepspace.typert.errors import RemoteError
from app.deepspace.typert.resources import ResourceAccess

F = TypeVar("F", bound=Callable[..., Any])

_REMOTE_ATTRIBUTE = "__deepspace_remote__"
_SCOPE_ATTRIBUTE = "__deepspace_remote_scope__"


def remote(
    *,
    name: str | None = None,
    namespace: str | None = None,
    mode: str = "unary",
    parameters: tuple[RemoteParameter, ...] | None = None,
    uplink_codec: Codec[Any] | None = None,
    cancellation: str | None = None,
    result_codec: Codec[Any] | None = None,
    scope: str | None = None,
    resources: tuple[ResourceAccess, ...] = (),
    description: str | None = None,
) -> Callable[[F], F]:
    """Mark one service method as a capability endpoint.

    Args:
        name: The wire method name; defaults to the Python method name.
        namespace: The endpoint namespace; defaults to the service key.
        mode: ``unary`` or ``stream``.
        parameters: Explicit parameter declarations; defaults to the
            method signature.
        uplink_codec: The codec of an uplink frame for a stream.
        cancellation: The cancellation parameter name; it must be ``signal``.
        result_codec: The result codec; defaults to the return annotation.
        scope: The scope projection this endpoint runs under.
        resources: The resources every call touches.
        description: The model-facing description; defaults to the
            docstring.

    Returns:
        The decorator.
    """
    if mode not in {"unary", "stream"}:
        raise ValueError("A capability mode is 'unary' or 'stream'.")
    if cancellation is not None and cancellation != "signal":
        raise ValueError("The cancellation parameter must be named 'signal'.")

    options = RemoteMethodOptions(
        name=name,
        namespace=namespace,
        mode=cast(Any, mode),
        parameters=parameters,
        uplink_codec=uplink_codec,
        cancellation_parameter=cancellation,
        result_codec=result_codec,
        scope=scope,
        resources=tuple(resources),
        description=description,
    )

    def decorate(function: F) -> F:
        if getattr(function, _REMOTE_ATTRIBUTE, None) is not None:
            raise TypeError(f"{function.__qualname__} is already a capability endpoint.")
        setattr(function, _REMOTE_ATTRIBUTE, options)
        return function

    return decorate


def remote_scope(scope: str) -> Callable[[F], F]:
    """Declare the scope one method or service runs under.

    Args:
        scope: The scope key.

    Returns:
        The decorator.
    """
    if not scope:
        raise ValueError("A capability scope needs a name.")

    def decorate(target: F) -> F:
        setattr(target, _SCOPE_ATTRIBUTE, scope)
        return target

    return decorate


@dataclass(frozen=True, slots=True)
class RemoteBinding:
    """One service instance's bound descriptors."""

    service_key: str
    service: object
    descriptors: tuple[RemoteDescriptor, ...]

    def get(self, endpoint: str) -> RemoteDescriptor | None:
        """Return one descriptor by endpoint."""
        return next((item for item in self.descriptors if item.id == endpoint), None)

    def as_dict(self) -> dict[str, Any]:
        """Return the inspectable binding description."""
        return {
            "service_key": self.service_key,
            "service": f"{type(self.service).__module__}.{type(self.service).__qualname__}",
            "descriptors": [descriptor.as_dict() for descriptor in self.descriptors],
        }


def bind_remote(service: object, service_key: str) -> RemoteBinding:
    """Collect every decorated method on one service instance.

    Args:
        service: The service instance.
        service_key: The stable service name used in endpoint ids.

    Returns:
        The bound descriptors, in method-definition order.

    Raises:
        TypeError: When a class is passed instead of an instance.
        RemoteError: When a method declaration is invalid.
    """
    if inspect.isclass(service):
        raise TypeError("bind_remote needs a service instance; pass an instance, not a class.")
    if not service_key or not service_key.isidentifier():
        raise ValueError("A service key must be a non-empty Python identifier.")

    service_type = type(service)
    descriptors: list[RemoteDescriptor] = []
    type_hints = _safe_type_hints(service_type)
    for name, static_member in inspect.getmembers_static(service_type):
        if name.startswith("_"):
            continue
        if isinstance(static_member, staticmethod | classmethod | property):
            if getattr(static_member, _REMOTE_ATTRIBUTE, None) is not None:
                raise RemoteError(
                    "gateway/internal",
                    f"Capability method {name!r} cannot be static, a class method, or a property.",
                )
            continue
        function = static_member
        options = getattr(function, _REMOTE_ATTRIBUTE, None)
        if options is None:
            continue
        if not callable(function):
            raise RemoteError(
                "gateway/internal",
                f"Capability method {name!r} is not callable.",
            )
        implementation = getattr(service, name)
        parameters = (
            tuple(options.parameters)
            if options.parameters is not None
            else _parameters_from_signature(function, type_hints)
        )
        scope = options.scope or getattr(function, _SCOPE_ATTRIBUTE, None)
        result_codec = options.result_codec or _result_codec(function, type_hints)
        descriptor = RemoteDescriptor(
            id=f"{options.namespace or service_key}/{options.name or name}",
            service=service_key,
            namespace=options.namespace or service_key,
            method=options.name or name,
            implementation=implementation,
            mode=options.mode,
            parameters=parameters,
            uplink_codec=options.uplink_codec,
            cancellation_parameter=options.cancellation_parameter,
            result_codec=result_codec,
            scope=scope,
            description=options.description or inspect.getdoc(function),
            resources=options.resources,
            source=f"{function.__module__}.{function.__qualname__}",
        )
        descriptor.validate()
        descriptors.append(descriptor)

    if not descriptors:
        raise RemoteError(
            "gateway/not-found",
            f"Service {service_key!r} has no capability methods.",
        )
    return RemoteBinding(service_key=service_key, service=service, descriptors=tuple(descriptors))


class TypertRemoteService:
    """Base class for a service that exposes decorated capability methods.

    A subclass names its service key once::

        class WorkspaceService(TypertRemoteService, service_key="workspace"):
            @remote()
            def read(self, path: str) -> str: ...

    The key may also be supplied when the binding is created.
    """

    typert_service_key: ClassVar[str | None] = None

    def __init_subclass__(cls, *, service_key: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if service_key is not None:
            if not service_key or not service_key.isidentifier():
                raise ValueError("A service key must be a non-empty Python identifier.")
            cls.typert_service_key = service_key

    def typert_binding(self, service_key: str | None = None) -> RemoteBinding:
        """Bind this instance's decorated methods."""
        key = service_key or self.typert_service_key or type(self).__name__
        binding = bind_remote(self, key)
        self._typert_binding = binding
        return binding


def _safe_type_hints(function: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(function)
    except (NameError, TypeError):
        return {}


def _parameters_from_signature(
    function: Callable[..., Any],
    type_hints: dict[str, Any],
) -> tuple[RemoteParameter, ...]:
    signature = inspect.signature(function)
    parameters: list[RemoteParameter] = []
    for index, parameter in enumerate(signature.parameters.values()):
        if index == 0 and parameter.name in {"self", "cls"}:
            continue
        if parameter.name == "signal":
            continue
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            raise RemoteError(
                "gateway/internal",
                f"Capability method {function.__qualname__} cannot use *args or **kwargs.",
            )
        annotation = type_hints.get(parameter.name, parameter.annotation)
        parameters.append(
            RemoteParameter(
                name=parameter.name,
                codec=_codec_for_annotation(annotation),
                accepts_undefined=parameter.default is not inspect.Parameter.empty,
            )
        )
    return tuple(parameters)


def _result_codec(
    function: Callable[..., Any],
    type_hints: dict[str, Any],
) -> Codec[Any]:
    annotation = type_hints.get("return", inspect.signature(function).return_annotation)
    if annotation is inspect.Signature.empty or annotation is None:
        return JsonCodec(type_symbol="null")
    return _codec_for_annotation(annotation)


def _codec_for_annotation(annotation: Any) -> Codec[Any]:
    if annotation is inspect.Signature.empty:
        return JsonCodec(type_symbol="json")
    return codec_for(annotation)


__all__ = [
    "RemoteBinding",
    "TypertRemoteService",
    "bind_remote",
    "remote",
    "remote_scope",
]
