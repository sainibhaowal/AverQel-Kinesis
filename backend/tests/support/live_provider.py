from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LiveProviderSettings:
    """Generic environment settings for live provider proofs."""

    provider_type: str = ""
    base_url: str = ""
    model: str = ""
    second_model: str = ""
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> LiveProviderSettings:
        """Read generic live-provider settings without requiring credentials."""
        values = os.environ if environ is None else environ

        def read(name: str) -> str:
            return str(values.get(name) or "").strip()

        return cls(
            provider_type=read("AVERQEL_LIVE_PROVIDER_TYPE"),
            base_url=read("AVERQEL_LIVE_BASE_URL"),
            model=read("AVERQEL_LIVE_MODEL"),
            second_model=read("AVERQEL_LIVE_SECOND_MODEL"),
            api_key=read("AVERQEL_LIVE_API_KEY") or None,
        )

    @property
    def is_configured(self) -> bool:
        """Whether the provider route can be selected without implicit defaults."""
        return bool(self.provider_type and self.base_url and self.model)

    @property
    def effective_second_model(self) -> str:
        """Use the primary model when a local runtime exposes only one model."""
        return self.second_model or self.model

    @property
    def skip_reason(self) -> str:
        """Explain the explicit settings needed to enable live-provider proofs."""
        return (
            "Set AVERQEL_LIVE_PROVIDER_TYPE, AVERQEL_LIVE_BASE_URL, and "
            "AVERQEL_LIVE_MODEL to enable live-provider proofs. "
            "AVERQEL_LIVE_API_KEY is optional for local or unauthenticated providers; "
            "AVERQEL_LIVE_SECOND_MODEL is optional."
        )
