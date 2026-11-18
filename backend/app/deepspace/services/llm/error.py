"""Error types for DeepSpace LLM service."""

from __future__ import annotations

from typing import Any

from app.deepspace.services.llm.brand import ProviderRequestId

# Error codes
INVALID_CREDENTIAL_CODE = "INVALID_CREDENTIAL"
NO_ADAPTER_CODE = "NO_ADAPTER"
DUPLICATE_ADAPTER_CODE = "DUPLICATE_ADAPTER"
INVALID_ADAPTER_CODE = "INVALID_ADAPTER"
REGISTRATION_DISPOSED_CODE = "REGISTRATION_DISPOSED"
INVALID_DIRECTORY_CODE = "INVALID_DIRECTORY"
DUPLICATE_DIRECTORY_CODE = "DUPLICATE_DIRECTORY"
NO_DISCOVERY_CODE = "NO_DISCOVERY"
INVALID_DISCOVERY_CODE = "INVALID_DISCOVERY"
INVALID_CATALOG_CODE = "INVALID_CATALOG"
INVALID_MODEL_INFO_CODE = "INVALID_MODEL_INFO"
INVALID_MODEL_CONTEXT_CODE = "INVALID_MODEL_CONTEXT"
INVALID_MODEL_MAX_TOKENS_CODE = "INVALID_MODEL_MAX_TOKENS"
INVALID_MODEL_REASONING_CODE = "INVALID_MODEL_REASONING"
UNSUPPORTED_REASONING_EFFORT_CODE = "UNSUPPORTED_REASONING_EFFORT"
INVALID_PREPARED_CALL_CODE = "INVALID_PREPARED_CALL"


class HarnessError(Exception):
    """Base error for harness failures."""

    def __init__(self, message: str, code: str, **kwargs: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = kwargs


class LlmError(HarnessError):
    """Typed error for LLM-related failures."""

    def __init__(
        self,
        message: str,
        code: str,
        *,
        status: int | None = None,
        provider_retry_after_ms: int | None = None,
        request_id: ProviderRequestId | None = None,
        offload_images: int | None = None,
    ) -> None:
        if not message:
            raise ValueError("LlmError message must be non-empty")
        if not code:
            raise ValueError("LlmError code must be non-empty")
        if status is not None and not (100 <= status <= 599):
            raise ValueError("LlmError status must be 100-599")
        if provider_retry_after_ms is not None and provider_retry_after_ms <= 0:
            raise ValueError("providerRetryAfterMs must be positive")

        super().__init__(
            message,
            code,
            **{
                "status": status,
                "providerRetryAfterMs": provider_retry_after_ms,
                "requestId": request_id,
                "offloadImages": offload_images,
            },
        )
        self.name = "LlmError"
        self.failure = {
            "message": message,
            "code": code,
            **({"status": status} if status is not None else {}),
            **(
                {"providerRetryAfterMs": provider_retry_after_ms}
                if provider_retry_after_ms is not None
                else {}
            ),
            **({"requestId": str(request_id)} if request_id is not None else {}),
            **({"offloadImages": offload_images} if offload_images is not None else {}),
        }


def assert_usable_api_key(raw: str, pkg: str, ref: str) -> str:
    """Validate and normalize an API key."""
    checked = raw.strip()
    if not checked:
        raise LlmError(
            f"{pkg}: the API key resolved from {ref} is blank; set {ref} to the raw key",
            INVALID_CREDENTIAL_CODE,
        )
    # Check for characters that can't be in HTTP headers
    if any(ord(c) > 127 or c in "\r\n" for c in checked):
        raise LlmError(
            f"{pkg}: the API key resolved from {ref} contains characters no HTTP header can carry",
            INVALID_CREDENTIAL_CODE,
        )
    return checked


def normalize_api_key(raw: str) -> tuple[bool, str, str]:
    """Normalize an API key, returning (ok, value, reason)."""
    try:
        value = assert_usable_api_key(raw, "llm", "api_key")
        return True, value, ""
    except LlmError as e:
        return False, "", e.details.get("reason", str(e))


__all__ = [
    "HarnessError",
    "LlmError",
    "assert_usable_api_key",
    "normalize_api_key",
    "INVALID_CREDENTIAL_CODE",
    "NO_ADAPTER_CODE",
    "DUPLICATE_ADAPTER_CODE",
    "INVALID_ADAPTER_CODE",
    "REGISTRATION_DISPOSED_CODE",
    "INVALID_DIRECTORY_CODE",
    "DUPLICATE_DIRECTORY_CODE",
    "NO_DISCOVERY_CODE",
    "INVALID_DISCOVERY_CODE",
    "INVALID_CATALOG_CODE",
    "INVALID_MODEL_INFO_CODE",
    "INVALID_MODEL_CONTEXT_CODE",
    "INVALID_MODEL_MAX_TOKENS_CODE",
    "INVALID_MODEL_REASONING_CODE",
    "UNSUPPORTED_REASONING_EFFORT_CODE",
    "INVALID_PREPARED_CALL_CODE",
]
