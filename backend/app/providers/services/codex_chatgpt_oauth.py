from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import jwt
from jwt import InvalidTokenError

from app.core.averqel_home import averqel_secrets_path
from app.core.config import Settings, get_settings
from app.providers.services.codex_cli_runtime import local_codex_cli_backend_enabled
from app.providers.services.provider_secret_crypto import ProviderSecretCrypto
from app.providers.services.reasoning_capabilities import normalize_reasoning_effort_value

logger = logging.getLogger(__name__)

OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_AUTHORIZE_URL = f"{OPENAI_ISSUER}/api/accounts/authorize"
OPENAI_TOKEN_URL = f"{OPENAI_ISSUER}/api/accounts/oauth/token"
OPENAI_DISCOVERY_URL = f"{OPENAI_ISSUER}/.well-known/openid-configuration"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_RESOURCE = "https://api.openai.com/v1"
OPENAI_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
OPENAI_MODEL_ID_PATTERN = re.compile(r"^[\w./:-]{1,255}$")
REQUIRED_PLAN_SCOPES = frozenset({"offline_access", "resource.invoke", "chatgpt.tokens.use.direct"})
STORE_AAD = b"averqel-openai-codex-chatgpt-accounts-v1"
PENDING_FLOW_TTL_SECONDS = 300
ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 60
MAX_OAUTH_ACCOUNTS = 16
MAX_OAUTH_STREAM_EVENT_BYTES = 1_000_000


class CodexChatGPTOAuthError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PendingOAuthFlow:
    flow_id: str
    tenant_id: str
    actor_user_id: str
    redirect_uri: str
    nonce: str
    verifier: str
    account_id: str | None
    client_id: str | None
    subject: str | None
    created_at: float


class CodexChatGPTOAuthRuntime:
    """Backend-owned Sign in with ChatGPT for the local Kinesis runtime."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        store_path: str | Path | None = None,
        crypto: ProviderSecretCrypto | None = None,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self.settings = settings or get_settings()
        self.store_path = (
            Path(store_path)
            if store_path
            else averqel_secrets_path() / ("openai-codex-accounts.enc")
        )
        self.crypto = crypto or ProviderSecretCrypto(self.settings)
        self.client_factory = client_factory
        self._store_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, PendingOAuthFlow] = {}
        self._completed: dict[str, dict[str, Any]] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._jwks_cache: tuple[float, dict[str, Any]] | None = None

    def available(self) -> bool:
        return local_codex_cli_backend_enabled(self.settings.redis_url)

    def accounts(self, *, tenant_id: UUID, actor_user_id: UUID) -> list[dict[str, Any]]:
        if not self.available():
            return []
        store = self._read_store()
        return [
            self._public_account(account)
            for account in store["accounts"]
            if account.get("tenant_id") == str(tenant_id)
            and account.get("actor_user_id") == str(actor_user_id)
        ]

    def has_account(self, *, account_id: str, tenant_id: str, actor_user_id: str) -> bool:
        if not self.available():
            return False
        account = self._find_account(account_id)
        return bool(
            account
            and account.get("tenant_id") == tenant_id
            and account.get("actor_user_id") == actor_user_id
        )

    def start(
        self,
        *,
        tenant_id: UUID,
        actor_user_id: UUID,
        redirect_uri: str,
        account_id: str | None = None,
    ) -> tuple[str, str]:
        self._require_available()
        parsed = httpx.URL(redirect_uri)
        if (
            parsed.scheme != "http"
            or parsed.host != "127.0.0.1"
            or parsed.port is None
            or not parsed.path.endswith("/providers/oauth/openai/callback")
            or parsed.query
            or parsed.fragment
        ):
            raise CodexChatGPTOAuthError(
                "ChatGPT sign-in requires the local Kinesis loopback callback.",
                status_code=400,
            )

        existing = None
        if account_id:
            existing = self._find_account(account_id)
            if (
                existing is None
                or existing.get("tenant_id") != str(tenant_id)
                or existing.get("actor_user_id") != str(actor_user_id)
            ):
                raise CodexChatGPTOAuthError(
                    "The selected ChatGPT account is not available to this user.",
                    status_code=404,
                )

        store = self._read_store()
        now = time.time()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        pending = PendingOAuthFlow(
            flow_id=str(uuid4()),
            tenant_id=str(tenant_id),
            actor_user_id=str(actor_user_id),
            redirect_uri=redirect_uri,
            nonce=nonce,
            verifier=verifier,
            account_id=account_id,
            client_id=existing.get("client_id") if existing else None,
            subject=existing.get("subject") if existing else None,
            created_at=now,
        )
        with self._pending_lock:
            self._pending = {
                key: flow
                for key, flow in self._pending.items()
                if now - flow.created_at <= PENDING_FLOW_TTL_SECONDS
            }
            self._completed = {
                key: value
                for key, value in self._completed.items()
                if now - float(value.get("created_at", 0)) <= PENDING_FLOW_TTL_SECONDS
            }
            if len(self._pending) >= 8:
                raise CodexChatGPTOAuthError(
                    "Too many ChatGPT sign-in attempts are already in progress.",
                    status_code=429,
                )
            self._pending[state] = pending

        params: dict[str, str] = {
            "client_id": pending.client_id or "dynamic_agent_client",
            "ext_agent_host_id": store["host_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
            "resource": OPENAI_RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if pending.client_id is None:
            params["agent_name_hint"] = "AverQel Kinesis"
        elif existing:
            if existing.get("id_token"):
                params["id_token_hint"] = str(existing["id_token"])
            if existing.get("email"):
                params["login_hint"] = str(existing["email"])
        return f"{OPENAI_AUTHORIZE_URL}?{urlencode(params)}", pending.flow_id

    def flow_status(self, *, flow_id: str, tenant_id: UUID, actor_user_id: UUID) -> dict[str, Any]:
        try:
            UUID(flow_id)
        except (TypeError, ValueError) as exc:
            raise CodexChatGPTOAuthError(
                "The ChatGPT sign-in attempt is invalid.", status_code=404
            ) from exc
        with self._pending_lock:
            completed = self._completed.get(flow_id)
            if completed:
                if completed.get("tenant_id") != str(tenant_id) or completed.get(
                    "actor_user_id"
                ) != str(actor_user_id):
                    raise CodexChatGPTOAuthError(
                        "The ChatGPT sign-in attempt is not available.", status_code=404
                    )
                return {
                    "status": completed["status"],
                    "message": completed.get("message"),
                    "account_id": (
                        completed.get("account", {}).get("id")
                        if isinstance(completed.get("account"), dict)
                        else None
                    ),
                }
            pending = next(
                (item for item in self._pending.values() if item.flow_id == flow_id),
                None,
            )
        if (
            pending is None
            or pending.tenant_id != str(tenant_id)
            or pending.actor_user_id != str(actor_user_id)
        ):
            raise CodexChatGPTOAuthError(
                "The ChatGPT sign-in attempt was not found.", status_code=404
            )
        return {"status": "pending", "message": None, "account_id": None}

    async def complete_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        callback_client_id: str | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Complete an OAuth callback, including dynamic registration client IDs."""
        self._require_available()
        if not isinstance(state, str) or not state or len(state) > 256:
            raise CodexChatGPTOAuthError(
                "ChatGPT sign-in state is missing or invalid.", status_code=400
            )
        with self._pending_lock:
            pending = self._pending.pop(state, None)
        if pending is None or time.time() - pending.created_at > PENDING_FLOW_TTL_SECONDS:
            raise CodexChatGPTOAuthError(
                "ChatGPT sign-in expired or was already used. Start again.",
                status_code=400,
            )
        if error:
            message = (
                "ChatGPT plan access was not authorized."
                if error == "access_denied"
                else "OpenAI sign-in was not completed."
            )
            self._set_flow_result(pending, "failed", None, message)
            raise CodexChatGPTOAuthError(message, status_code=400)
        if not isinstance(code, str) or not code or len(code) > 4096:
            self._set_flow_result(
                pending,
                "failed",
                None,
                "OpenAI returned an incomplete sign-in callback.",
            )
            raise CodexChatGPTOAuthError(
                "OpenAI returned an incomplete sign-in callback.", status_code=400
            )

        try:
            return await self._complete_callback(
                pending=pending,
                code=code,
                callback_client_id=callback_client_id,
            )
        except CodexChatGPTOAuthError as exc:
            self._set_flow_result(pending, "failed", None, str(exc))
            raise
        except Exception as exc:  # pragma: no cover - defensive failure boundary
            self._set_flow_result(
                pending,
                "failed",
                None,
                "ChatGPT sign-in could not be completed. Start again.",
            )
            raise CodexChatGPTOAuthError(
                "ChatGPT sign-in could not be completed. Start again.",
                status_code=502,
            ) from exc

    async def _complete_callback(
        self,
        *,
        pending: PendingOAuthFlow,
        code: str | None,
        callback_client_id: str | None,
    ) -> dict[str, Any]:
        if not isinstance(code, str) or not code or len(code) > 4096:
            raise CodexChatGPTOAuthError(
                "OpenAI returned an incomplete sign-in callback.", status_code=400
            )
        issued_client_id = pending.client_id or callback_client_id
        if (
            not isinstance(issued_client_id, str)
            or not OPENAI_CLIENT_ID_PATTERN.fullmatch(issued_client_id)
            or issued_client_id == "dynamic_agent_client"
            or (pending.client_id and callback_client_id not in {None, pending.client_id})
        ):
            raise CodexChatGPTOAuthError(
                "OpenAI did not complete the ChatGPT account registration.",
                status_code=400,
            )
        token_response = await self._post_form(
            OPENAI_TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": issued_client_id,
                "code": code,
                "code_verifier": pending.verifier,
                "redirect_uri": pending.redirect_uri,
                "resource": OPENAI_RESOURCE,
            },
        )
        account = await self._finish_callback(
            pending=pending,
            token_response=token_response,
            callback_client_id=issued_client_id,
        )
        public_account = {key: value for key, value in account.items() if not key.startswith("_")}
        self._set_flow_result(pending, "complete", public_account, None)
        account["_tenant_id"] = pending.tenant_id
        account["_actor_user_id"] = pending.actor_user_id
        return account

    def _set_flow_result(
        self,
        pending: PendingOAuthFlow,
        status: str,
        account: dict[str, Any] | None,
        message: str | None,
    ) -> None:
        with self._pending_lock:
            self._completed[pending.flow_id] = {
                "created_at": time.time(),
                "tenant_id": pending.tenant_id,
                "actor_user_id": pending.actor_user_id,
                "status": status,
                "account": account,
                "message": message,
            }

    async def _finish_callback(
        self,
        *,
        pending: PendingOAuthFlow,
        token_response: dict[str, Any],
        callback_client_id: str | None,
    ) -> dict[str, Any]:
        client_id = callback_client_id or pending.client_id
        if not isinstance(client_id, str) or not OPENAI_CLIENT_ID_PATTERN.fullmatch(client_id):
            raise CodexChatGPTOAuthError(
                "OpenAI did not complete the ChatGPT account registration.", status_code=400
            )
        access_token = token_response.get("access_token")
        refresh_token = token_response.get("refresh_token")
        id_token = token_response.get("id_token")
        expires_in = token_response.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
            or not isinstance(id_token, str)
            or not id_token
            or not isinstance(expires_in, int | float)
            or not 0 < expires_in <= 31_536_000
        ):
            raise CodexChatGPTOAuthError("OpenAI returned an incomplete token response.")
        raw_scopes = token_response.get("scope")
        scopes = set(raw_scopes.split()) if isinstance(raw_scopes, str) else set()
        if not REQUIRED_PLAN_SCOPES.issubset(scopes):
            raise CodexChatGPTOAuthError(
                "ChatGPT plan usage permission was not granted. Sign in again and approve model usage.",
                status_code=403,
            )
        claims = await self._validate_id_token(
            id_token,
            client_id=client_id,
            nonce=pending.nonce,
            expected_subject=pending.subject,
        )
        issuer = str(claims["iss"])
        subject = str(claims["sub"])
        account_id = hashlib.sha256(f"{issuer}\0{subject}\0{client_id}".encode()).hexdigest()[:32]
        store = self._read_store()
        account = {
            "id": account_id,
            "tenant_id": pending.tenant_id,
            "actor_user_id": pending.actor_user_id,
            "issuer": issuer,
            "subject": subject,
            "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
            "name": claims.get("name") if isinstance(claims.get("name"), str) else None,
            "client_id": client_id,
            "host_id": store["host_id"],
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "scopes": sorted(scopes),
            "expires_at": time.time() + float(expires_in),
        }
        with self._store_lock:
            current = self._read_store()
            accounts = current["accounts"]
            existing_index = next(
                (
                    index
                    for index, item in enumerate(accounts)
                    if item.get("id") == account_id
                    and item.get("tenant_id") == pending.tenant_id
                    and item.get("actor_user_id") == pending.actor_user_id
                ),
                None,
            )
            if existing_index is None and len(accounts) >= MAX_OAUTH_ACCOUNTS:
                raise CodexChatGPTOAuthError(
                    "This local installation already has the maximum number of ChatGPT accounts.",
                    status_code=409,
                )
            if existing_index is None:
                accounts.append(account)
            else:
                accounts[existing_index] = account
            self._write_store(current)
        public = self._public_account(account)
        public["_tenant_id"] = pending.tenant_id
        public["_actor_user_id"] = pending.actor_user_id
        return public

    async def models(
        self, *, account_id: str, tenant_id: str, actor_user_id: str
    ) -> list[dict[str, Any]]:
        access_token = await self.access_token(
            account_id=account_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )
        try:
            async with self.client_factory(timeout=20.0, follow_redirects=False) as client:
                response = await client.get(
                    OPENAI_MODELS_URL,
                    headers={
                        "authorization": f"Bearer {access_token}",
                        "accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise CodexChatGPTOAuthError(
                "The ChatGPT model list could not be reached. Check the network and retry.",
                status_code=503,
            ) from exc
        if not response.is_success:
            raise self._response_error(response, "The ChatGPT model list request failed.")
        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise CodexChatGPTOAuthError("OpenAI returned an invalid model catalog.") from exc
        raw_models = body.get("models") if isinstance(body, dict) else None
        if not isinstance(raw_models, list):
            raise CodexChatGPTOAuthError("OpenAI returned an invalid model catalog.")
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_models[:1000]:
            if (
                not isinstance(item, dict)
                or item.get("visibility") != "list"
                or not isinstance(item.get("slug"), str)
                or not OPENAI_MODEL_ID_PATTERN.fullmatch(item["slug"])
                or item["slug"] in seen
            ):
                continue
            slug = item["slug"]
            seen.add(slug)
            display_name = item.get("display_name")
            model: dict[str, Any] = {
                "id": slug,
                "name": (
                    display_name.strip()[:255]
                    if isinstance(display_name, str) and display_name.strip()
                    else slug
                ),
                "kind": "chat",
            }
            efforts = item.get("supported_reasoning_efforts")
            if isinstance(efforts, list):
                native_values: dict[str, str] = {}
                for effort in efforts[:32]:
                    if not isinstance(effort, str):
                        continue
                    canonical = normalize_reasoning_effort_value(effort)
                    if canonical:
                        native_values.setdefault(canonical, effort.strip())
                if native_values:
                    model["supportedReasoningEfforts"] = list(native_values)
                    model["reasoningEffortValues"] = native_values
            models.append(model)
        if not models:
            raise CodexChatGPTOAuthError(
                "OpenAI did not return any models available to this ChatGPT account.",
                status_code=403,
            )
        return models

    async def access_token(self, *, account_id: str, tenant_id: str, actor_user_id: str) -> str:
        self._require_available()
        key = f"{tenant_id}:{actor_user_id}:{account_id}"
        lock = self._refresh_locks.setdefault(key, asyncio.Lock())
        async with lock:
            account = self._scoped_account(
                account_id=account_id,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
            )
            if (
                float(account.get("expires_at") or 0)
                > time.time() + ACCESS_TOKEN_REFRESH_MARGIN_SECONDS
            ):
                token = account.get("access_token")
                if isinstance(token, str) and token:
                    return token
            refresh_token = account.get("refresh_token")
            client_id = account.get("client_id")
            if not isinstance(refresh_token, str) or not isinstance(client_id, str):
                raise CodexChatGPTOAuthError(
                    "The ChatGPT account needs to be signed in again.",
                    status_code=401,
                )
            token_response = await self._post_form(
                OPENAI_TOKEN_URL,
                {
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "resource": OPENAI_RESOURCE,
                },
            )
            new_access_token = token_response.get("access_token")
            expires_in = token_response.get("expires_in")
            if (
                not isinstance(new_access_token, str)
                or not new_access_token
                or not isinstance(expires_in, int | float)
                or not 0 < expires_in <= 31_536_000
            ):
                raise CodexChatGPTOAuthError(
                    "OpenAI returned an incomplete token refresh response.",
                    status_code=502,
                )
            new_refresh_token = token_response.get("refresh_token")
            new_scopes = token_response.get("scope")
            refreshed_scopes = (
                set(new_scopes.split())
                if isinstance(new_scopes, str)
                else set(account.get("scopes", []))
            )
            updated = {
                **account,
                "access_token": new_access_token,
                "refresh_token": (
                    new_refresh_token
                    if isinstance(new_refresh_token, str) and new_refresh_token
                    else refresh_token
                ),
                "expires_at": time.time() + float(expires_in),
                "scopes": sorted(refreshed_scopes),
            }
            self._replace_account(updated)
            if not REQUIRED_PLAN_SCOPES.issubset(refreshed_scopes):
                raise CodexChatGPTOAuthError(
                    "ChatGPT plan usage permission is no longer available. Sign in again and approve model usage.",
                    status_code=403,
                )
            return new_access_token

    async def stream_responses(
        self,
        *,
        account_id: str,
        tenant_id: str,
        actor_user_id: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        token = await self.access_token(
            account_id=account_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )
        request_payload = dict(payload)
        request_payload["store"] = False
        request_payload["stream"] = True
        try:
            async with self.client_factory(timeout=300.0, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    OPENAI_RESPONSES_URL,
                    headers={
                        "authorization": f"Bearer {token}",
                        "content-type": "application/json",
                        "accept": "text/event-stream",
                    },
                    json=request_payload,
                ) as response:
                    if not response.is_success:
                        body = await response.aread()
                        raise self._response_error(
                            response, "OpenAI Responses request failed.", body=body
                        )
                    completed = False
                    async for line in response.aiter_lines():
                        if len(line.encode("utf-8")) > MAX_OAUTH_STREAM_EVENT_BYTES:
                            raise CodexChatGPTOAuthError(
                                "OpenAI returned an oversized streaming event.", status_code=502
                            )
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise CodexChatGPTOAuthError(
                                "OpenAI returned an invalid streaming event.", status_code=502
                            ) from exc
                        if isinstance(event, dict):
                            event_type = event.get("type")
                            if event_type in {"response.failed", "response.incomplete", "error"}:
                                # The provider adapter turns these into structured provider errors.
                                yield event
                                continue
                            if event_type == "response.completed":
                                completed = True
                            yield event
                    if not completed:
                        raise CodexChatGPTOAuthError(
                            "The OpenAI Responses stream ended before completion. Retry the request.",
                            status_code=502,
                        )
        except httpx.HTTPError as exc:
            raise CodexChatGPTOAuthError(
                "The OpenAI Responses stream was interrupted. Retry the request.",
                status_code=503,
            ) from exc

    async def disconnect(self, *, account_id: str, tenant_id: str, actor_user_id: str) -> bool:
        account = self._scoped_account(
            account_id=account_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
        )
        revoked = False
        try:
            discovery = await self._oidc_configuration()
            endpoint = discovery.get("revocation_endpoint")
            if (
                isinstance(endpoint, str)
                and endpoint.startswith(f"{OPENAI_ISSUER}/")
                and isinstance(account.get("refresh_token"), str)
            ):
                async with self.client_factory(timeout=15.0, follow_redirects=False) as client:
                    response = await client.post(
                        endpoint,
                        data={
                            "token": account["refresh_token"],
                            "token_type_hint": "refresh_token",
                            "client_id": account["client_id"],
                        },
                        headers={"content-type": "application/x-www-form-urlencoded"},
                    )
                    revoked = response.status_code == 200
        except (httpx.HTTPError, CodexChatGPTOAuthError):
            logger.info("OpenAI account revocation could not be confirmed.")
        self._remove_account(account_id, tenant_id, actor_user_id)
        return revoked

    async def _post_form(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        try:
            async with self.client_factory(timeout=20.0, follow_redirects=False) as client:
                response = await client.post(
                    url,
                    data=fields,
                    headers={
                        "content-type": "application/x-www-form-urlencoded",
                        "accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise CodexChatGPTOAuthError(
                "OpenAI sign-in could not reach the authorization service. Check the network and retry.",
                status_code=503,
            ) from exc
        if not response.is_success:
            raise self._response_error(response, "OpenAI rejected the sign-in request.")
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise CodexChatGPTOAuthError(
                "OpenAI returned an invalid authorization response."
            ) from exc
        if not isinstance(payload, dict):
            raise CodexChatGPTOAuthError("OpenAI returned an invalid authorization response.")
        return payload

    async def _oidc_configuration(self) -> dict[str, Any]:
        try:
            async with self.client_factory(timeout=10.0, follow_redirects=False) as client:
                response = await client.get(
                    OPENAI_DISCOVERY_URL,
                    headers={"accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise CodexChatGPTOAuthError(
                "OpenAI identity keys could not be reached. Check the network and retry.",
                status_code=503,
            ) from exc
        if not response.is_success:
            raise CodexChatGPTOAuthError(
                "OpenAI identity configuration is unavailable.", status_code=503
            )
        try:
            config = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise CodexChatGPTOAuthError(
                "OpenAI identity configuration is invalid.", status_code=502
            ) from exc
        if (
            not isinstance(config, dict)
            or config.get("issuer") != OPENAI_ISSUER
            or not isinstance(config.get("jwks_uri"), str)
            or not config["jwks_uri"].startswith(f"{OPENAI_ISSUER}/")
        ):
            raise CodexChatGPTOAuthError(
                "OpenAI identity configuration is invalid.", status_code=502
            )
        return config

    async def _validate_id_token(
        self,
        token: str,
        *,
        client_id: str,
        nonce: str,
        expected_subject: str | None,
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise CodexChatGPTOAuthError(
                "OpenAI returned an invalid identity token.", status_code=401
            ) from exc
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise CodexChatGPTOAuthError(
                "OpenAI identity token signing metadata is invalid.", status_code=401
            )
        config = await self._oidc_configuration()
        now = time.monotonic()
        jwks: dict[str, Any] | None = None
        if self._jwks_cache and now - self._jwks_cache[0] < 3600:
            jwks = self._jwks_cache[1]
        if jwks is None:
            try:
                async with self.client_factory(timeout=10.0, follow_redirects=False) as client:
                    response = await client.get(
                        config["jwks_uri"],
                        headers={"accept": "application/json"},
                    )
            except httpx.HTTPError as exc:
                raise CodexChatGPTOAuthError(
                    "OpenAI identity keys could not be reached. Check the network and retry.",
                    status_code=503,
                ) from exc
            if not response.is_success:
                raise CodexChatGPTOAuthError(
                    "OpenAI identity keys are unavailable.", status_code=503
                )
            try:
                jwks = response.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise CodexChatGPTOAuthError(
                    "OpenAI identity keys are invalid.", status_code=502
                ) from exc
            if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
                raise CodexChatGPTOAuthError("OpenAI identity keys are invalid.", status_code=502)
            self._jwks_cache = (now, jwks)
        key_data = next(
            (
                item
                for item in jwks["keys"]
                if isinstance(item, dict)
                and item.get("kid") == header["kid"]
                and item.get("use", "sig") == "sig"
            ),
            None,
        )
        if key_data is None:
            self._jwks_cache = None
            raise CodexChatGPTOAuthError(
                "OpenAI identity token signing key was not found.", status_code=401
            )
        try:
            key = jwt.PyJWK.from_dict(key_data, algorithm="RS256").key
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=OPENAI_ISSUER,
                leeway=60,
                options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
            )
        except InvalidTokenError as exc:
            raise CodexChatGPTOAuthError(
                "OpenAI identity token validation failed.", status_code=401
            ) from exc
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise CodexChatGPTOAuthError(
                "OpenAI identity token has no account subject.", status_code=401
            )
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise CodexChatGPTOAuthError("OpenAI sign-in nonce validation failed.", status_code=401)
        if claims.get("azp") is not None and claims.get("azp") != client_id:
            raise CodexChatGPTOAuthError(
                "OpenAI identity token authorized a different client.", status_code=401
            )
        audience = claims.get("aud")
        if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != client_id:
            raise CodexChatGPTOAuthError(
                "OpenAI identity token authorized a different client.", status_code=401
            )
        if expected_subject and claims["sub"] != expected_subject:
            raise CodexChatGPTOAuthError(
                "The ChatGPT account changed during reauthorization.", status_code=403
            )
        return claims

    def _read_store(self) -> dict[str, Any]:
        with self._store_lock:
            if not self.store_path.exists():
                store = {"version": 1, "host_id": f"urn:uuid:{uuid4()}", "accounts": []}
                self._write_store(store)
                return store
            try:
                envelope = json.loads(self.store_path.read_text(encoding="utf-8"))
                ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
                nonce = base64.urlsafe_b64decode(envelope["nonce"])
                plaintext = self.crypto.decrypt(
                    ciphertext=ciphertext,
                    nonce=nonce,
                    kid=str(envelope["kid"]),
                    aad=STORE_AAD,
                )
                store = json.loads(plaintext.decode("utf-8"))
            except Exception as exc:
                raise CodexChatGPTOAuthError(
                    "The encrypted local ChatGPT account store could not be read.",
                    status_code=503,
                ) from exc
            if (
                not isinstance(store, dict)
                or store.get("version") != 1
                or not isinstance(store.get("host_id"), str)
                or not isinstance(store.get("accounts"), list)
            ):
                raise CodexChatGPTOAuthError(
                    "The encrypted local ChatGPT account store is invalid.",
                    status_code=503,
                )
            return store

    def _write_store(self, store: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hasattr(os, "chmod"):
            self.store_path.parent.chmod(0o700)
        plaintext = json.dumps(store, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encrypted = self.crypto.encrypt(plaintext, aad=STORE_AAD)
        envelope = json.dumps(
            {
                "kid": encrypted.kid,
                "nonce": base64.urlsafe_b64encode(encrypted.nonce).decode("ascii"),
                "ciphertext": base64.urlsafe_b64encode(encrypted.ciphertext).decode("ascii"),
            },
            separators=(",", ":"),
        )
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".openai-codex-accounts-",
            suffix=".tmp",
            dir=self.store_path.parent,
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(envelope)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.store_path)
            self.store_path.chmod(0o600)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temporary_path).unlink(missing_ok=True)
            raise

    @staticmethod
    def _public_account(account: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": account.get("id"),
            "email": account.get("email"),
            "name": account.get("name"),
            "connected": True,
            "source": "chatgpt",
        }

    def _find_account(self, account_id: str) -> dict[str, Any] | None:
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{32}", account_id):
            return None
        return next(
            (item for item in self._read_store()["accounts"] if item.get("id") == account_id),
            None,
        )

    def _scoped_account(
        self, *, account_id: str, tenant_id: str, actor_user_id: str
    ) -> dict[str, Any]:
        account = self._find_account(account_id)
        if (
            account is None
            or account.get("tenant_id") != tenant_id
            or account.get("actor_user_id") != actor_user_id
        ):
            raise CodexChatGPTOAuthError(
                "The selected ChatGPT account is not available to this user.",
                status_code=403,
            )
        return account

    def _replace_account(self, account: dict[str, Any]) -> None:
        with self._store_lock:
            store = self._read_store()
            for index, current in enumerate(store["accounts"]):
                if (
                    current.get("id") == account.get("id")
                    and current.get("tenant_id") == account.get("tenant_id")
                    and current.get("actor_user_id") == account.get("actor_user_id")
                ):
                    store["accounts"][index] = account
                    self._write_store(store)
                    return
            raise CodexChatGPTOAuthError(
                "The selected ChatGPT account is no longer connected.", status_code=404
            )

    def _remove_account(self, account_id: str, tenant_id: str, actor_user_id: str) -> None:
        with self._store_lock:
            store = self._read_store()
            store["accounts"] = [
                account
                for account in store["accounts"]
                if not (
                    account.get("id") == account_id
                    and account.get("tenant_id") == tenant_id
                    and account.get("actor_user_id") == actor_user_id
                )
            ]
            self._write_store(store)

    def _require_available(self) -> None:
        if not self.available():
            raise CodexChatGPTOAuthError(
                "ChatGPT browser sign-in is available only in the standalone local Kinesis runtime.",
                status_code=400,
            )

    @staticmethod
    def _response_error(
        response: httpx.Response, fallback: str, *, body: bytes | None = None
    ) -> CodexChatGPTOAuthError:
        try:
            payload = json.loads(body if body is not None else response.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        detail = error.get("message") if isinstance(error, dict) else None
        message = detail.strip()[:300] if isinstance(detail, str) and detail.strip() else fallback
        status = response.status_code if 400 <= response.status_code <= 599 else 502
        if code == "subscription_sharing_usage_limit_exceeded":
            message = (
                "ChatGPT plan usage limit reached. Check ChatGPT Settings → Usage before retrying."
            )
            status = 429
        return CodexChatGPTOAuthError(message, status_code=status)


codex_chatgpt_oauth_runtime = CodexChatGPTOAuthRuntime()
