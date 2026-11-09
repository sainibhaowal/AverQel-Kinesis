from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator


class ProviderOAuthStartRequest(BaseModel):
    provider_type: str = "openai"
    account_id: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("provider_type")
    @classmethod
    def validate_provider_type(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned != "openai":
            raise ValueError("provider_type must be openai")
        return cleaned

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("account_id must be a valid local ChatGPT account identifier")
        return value


class ProviderOAuthStartResponse(BaseModel):
    available: bool
    authorization_url: str | None = None
    flow_id: str | None = None
    message: str

    model_config = ConfigDict(extra="forbid")


class ProviderOAuthCallbackResponse(BaseModel):
    connected: bool
    message: str

    model_config = ConfigDict(extra="forbid")


class ProviderOAuthStatusResponse(BaseModel):
    available: bool
    connected: bool
    provider_type: str
    message: str

    model_config = ConfigDict(extra="forbid")


class ProviderOAuthFlowStatusResponse(BaseModel):
    status: str
    message: str | None = None
    account_id: str | None = None

    model_config = ConfigDict(extra="forbid")
