"""Branded types for DeepSpace LLM service."""

from __future__ import annotations

from typing import TYPE_CHECKING, NewType

if TYPE_CHECKING:
    ProviderRequestId = NewType("ProviderRequestId", str)
    ModelModality = NewType("ModelModality", str)
    ProviderRoute = NewType("ProviderRoute", str)

from app.deepspace.utils.brand import branded

# Create branded types using the branded() function
ProviderRequestId = branded("ProviderRequestId", str)  # type: ignore[misc]
ModelModality = branded("ModelModality", str)  # type: ignore[misc]
ProviderRoute = branded("ProviderRoute", str)  # type: ignore[misc]


def provider_request_id(value: str) -> ProviderRequestId:
    return ProviderRequestId(value)


def model_modality(value: str) -> ModelModality:
    return ModelModality(value)


def provider_route(value: str) -> ProviderRoute:
    return ProviderRoute(value)


__all__ = [
    "ProviderRequestId",
    "provider_request_id",
    "ModelModality",
    "model_modality",
    "ProviderRoute",
    "provider_route",
]
