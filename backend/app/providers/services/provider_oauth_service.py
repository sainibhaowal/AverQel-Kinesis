from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ApiError
from app.providers.services.codex_chatgpt_oauth import (
    CodexChatGPTOAuthError,
    CodexChatGPTOAuthRuntime,
    codex_chatgpt_oauth_runtime,
)
from app.system.services.audit_service import AuditService


@dataclass(slots=True)
class ProviderOAuthService:
    db: Session
    settings: Settings
    runtime: CodexChatGPTOAuthRuntime = field(default_factory=lambda: codex_chatgpt_oauth_runtime)
    audit: AuditService = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "audit", AuditService(self.db))

    def status(self, *, tenant_id: UUID) -> tuple[bool, str]:
        del tenant_id
        if not self.runtime.available():
            return (
                False,
                "ChatGPT browser sign-in is available only in the standalone local Kinesis runtime.",
            )
        return True, "ChatGPT browser sign-in is available in the local Kinesis backend."

    def connected(self, *, tenant_id: UUID, actor_user_id: UUID) -> bool:
        try:
            return bool(self.runtime.accounts(tenant_id=tenant_id, actor_user_id=actor_user_id))
        except CodexChatGPTOAuthError as exc:
            raise ApiError(
                code="PROVIDER_OAUTH_UNSUPPORTED",
                message=str(exc),
                status_code=exc.status_code,
            ) from exc

    def start(
        self,
        *,
        tenant_id: UUID,
        actor_user_id: UUID,
        redirect_uri: str,
        account_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        available, message = self.status(tenant_id=tenant_id)
        if not available:
            raise ApiError(
                code="PROVIDER_OAUTH_UNSUPPORTED",
                message=message,
                status_code=400,
            )
        try:
            authorization_url, flow_id = self.runtime.start(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                redirect_uri=redirect_uri,
                account_id=account_id,
            )
        except CodexChatGPTOAuthError as exc:
            raise ApiError(
                code="PROVIDER_OAUTH_UNSUPPORTED",
                message=str(exc),
                status_code=exc.status_code,
            ) from exc
        self.audit.write_event(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="provider.oauth.start",
            resource_type="provider_oauth",
            details={"provider_type": "openai"},
        )
        return True, authorization_url, flow_id, "ChatGPT sign-in opened in the system browser."

    def flow_status(
        self, *, flow_id: str, tenant_id: UUID, actor_user_id: UUID
    ) -> dict[str, str | None]:
        try:
            status = self.runtime.flow_status(
                flow_id=flow_id,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
            )
        except CodexChatGPTOAuthError as exc:
            raise ApiError(
                code="PROVIDER_OAUTH_UNSUPPORTED",
                message=str(exc),
                status_code=exc.status_code,
            ) from exc
        return {
            "status": str(status.get("status") or "pending"),
            "message": status.get("message"),
            "account_id": status.get("account_id"),
        }

    async def callback(
        self,
        *,
        code: str | None,
        state: str | None,
        callback_client_id: str | None,
        error: str | None,
    ) -> tuple[bool, str]:
        try:
            result = await self.runtime.complete_callback(
                code=code,
                state=state,
                callback_client_id=callback_client_id,
                error=error,
            )
        except CodexChatGPTOAuthError as exc:
            raise ApiError(
                code="PROVIDER_OAUTH_UNSUPPORTED",
                message=str(exc),
                status_code=exc.status_code,
            ) from exc
        try:
            tenant_id = UUID(str(result.pop("_tenant_id")))
            actor_user_id = UUID(str(result.pop("_actor_user_id")))
        except (TypeError, ValueError) as exc:  # pragma: no cover - runtime contract guard
            raise ApiError(
                code="PROVIDER_OAUTH_UNSUPPORTED",
                message="ChatGPT sign-in completed without a valid local owner.",
                status_code=500,
            ) from exc
        self.audit.write_event(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="provider.oauth.callback",
            resource_type="provider_oauth",
            details={"provider_type": "openai"},
        )
        return True, "ChatGPT account connected. Return to AverQel to choose a model."

    async def disconnect(self, *, tenant_id: UUID, actor_user_id: UUID, account_id: str) -> bool:
        try:
            revoked = await self.runtime.disconnect(
                account_id=account_id,
                tenant_id=str(tenant_id),
                actor_user_id=str(actor_user_id),
            )
        except CodexChatGPTOAuthError as exc:
            raise ApiError(
                code=(
                    "PROVIDER_NOT_FOUND" if exc.status_code == 403 else "PROVIDER_OAUTH_UNSUPPORTED"
                ),
                message=str(exc),
                status_code=exc.status_code,
            ) from exc
        self.audit.write_event(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="provider.oauth.disconnect",
            resource_type="provider_oauth",
            details={"provider_type": "openai", "revocation_confirmed": revoked},
        )
        return revoked
