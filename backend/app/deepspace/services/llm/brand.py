"""Branded types for DeepSpace LLM service."""

from __future__ import annotations

from app.deepspace.utils.brand import branded

# Create branded types using the branded() function
ProviderRequestId = branded("ProviderRequestId", str)
ModelModality = branded("ModelModality", str)
ProviderRoute = branded("ProviderRoute", str)


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
