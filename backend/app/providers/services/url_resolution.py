from __future__ import annotations


def resolve_provider_base_url(
    base_url: str | None, *, provider_type: str | None = None
) -> str | None:
    del provider_type
    if base_url is None:
        return None

    return base_url.strip().rstrip("/")
