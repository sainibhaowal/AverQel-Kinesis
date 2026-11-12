"""Composio-brokered MCP apps.

Composio hosts one MCP session per user, and that session owns the OAuth
credentials for every toolkit it is authorized against. AverQel therefore
stores no provider OAuth client for Composio apps: it stores the encrypted
Composio session id, resolves the session into a live MCP endpoint when a
runtime is built, and never persists the endpoint or its headers.

The Composio API key is deployment configuration, not per-connection state.
It is read from ``~/.averqel-harness/secrets/composio_key.txt`` first, then the
``composio_api_key`` setting, then ``COMPOSIO_API_KEY``.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.integrations.models.mcp_server import MCPServer
from app.integrations.repositories.mcp_events import MCPEventsRepository
from app.integrations.services.connector_secret_crypto import ConnectorSecretCrypto
from app.integrations.services.mcp_endpoint_security import validate_remote_endpoint
from app.platform.database.session import set_db_tenant_context

logger = logging.getLogger(__name__)

COMPOSIO_PROVIDER_PREFIX = "composio:"
COMPOSIO_API_HOST = "backend.composio.dev"
# Hosts used by earlier AverQel catalogs to advertise Composio-brokered apps.
# They never served MCP directly; the toolkit slug beside them is the real
# connection input, so entries pinned to these hosts stay routable.
COMPOSIO_LEGACY_CATALOG_HOSTS = frozenset({"api.composio.dev"})
COMPOSIO_TOOLKIT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
COMPOSIO_SECRET_FILENAME = "composio_key.txt"
COMPOSIO_CALLBACK_PATH = "/dashboard/mcp/composio/callback"
_COMPOSIO_RETURN_HEADER_ALLOWLIST = frozenset(
    {"x-api-key", "x-user-api-key", "x-org-id", "x-project-id"}
)


class ComposioConfigurationError(RuntimeError):
    """A safe-to-display Composio configuration or catalog error."""


@dataclass(frozen=True, slots=True)
class ComposioConnectionStart:
    """Outcome of starting an authorization for one Composio app."""

    server: MCPServer
    authorization_url: str | None
    already_connected: bool = False
    authorization_required: bool = True


def composio_user_id(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Derive a stable, non-email Composio identity from the authenticated owner."""
    return f"averqel:{tenant_id}:{user_id}"


def composio_provider_slug(toolkit_slug: str) -> str:
    """Return the AverQel provider slug that marks a Composio-brokered app."""
    return f"{COMPOSIO_PROVIDER_PREFIX}{toolkit_slug}"


def composio_toolkit_slug(provider_slug: str | None) -> str | None:
    """Return the Composio toolkit slug a provider slug refers to, if any."""
    value = str(provider_slug or "")
    if not value.startswith(COMPOSIO_PROVIDER_PREFIX):
        return None
    slug = value[len(COMPOSIO_PROVIDER_PREFIX) :].strip().lower()
    return slug or None


def is_composio_server(server: MCPServer) -> bool:
    """Return whether a durable MCP server is brokered by Composio."""
    return composio_toolkit_slug(getattr(server, "provider_slug", None)) is not None


def is_composio_host(host: str | None) -> bool:
    """Return whether a URL host belongs to Composio's MCP gateway."""
    value = str(host or "").strip().lower()
    return (
        value == COMPOSIO_API_HOST
        or value.endswith(f".{COMPOSIO_API_HOST}")
        or value in COMPOSIO_LEGACY_CATALOG_HOSTS
    )


def composio_toolkit_for_entry(entry: Any) -> str | None:
    """Return the Composio toolkit a catalog entry brokers, if it brokers one.

    Catalog entries written by the current seeder carry the toolkit slug in
    ``package_metadata``. Entries seeded from the earlier ``api.composio.dev``
    catalog carry it as their provider slug instead, so both remain routable
    without a catalog rewrite.
    """
    package = getattr(entry, "package_metadata", None)
    if isinstance(package, dict):
        candidate = str(package.get("composio_toolkit") or "").strip().lower()
        if candidate and COMPOSIO_TOOLKIT_SLUG_PATTERN.fullmatch(candidate):
            return candidate
    declared = str(getattr(entry, "provider_slug", None) or "").strip().lower()
    if COMPOSIO_TOOLKIT_SLUG_PATTERN.fullmatch(declared) and is_composio_host(
        urlsplit(str(getattr(entry, "remote_url", None) or "")).hostname
    ):
        return declared
    return None


def composio_secrets_path() -> Path:
    """Return the on-disk location of the locally saved Composio API key."""
    from app.core.averqel_home import averqel_secrets_path

    return averqel_secrets_path() / COMPOSIO_SECRET_FILENAME


def composio_callback_url(public_origin: str | None, toolkit_slug: str) -> str | None:
    """Build the user-facing OAuth redirect URL from AverQel's configured origin."""
    origin = (public_origin or "").strip().rstrip("/")
    try:
        parsed = urlsplit(origin)
        hostname = (parsed.hostname or "").lower()
        local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not origin
            or (parsed.scheme != "https" and not local_http)
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
        ):
            return None
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                COMPOSIO_CALLBACK_PATH,
                urlencode({"toolkit_slug": toolkit_slug}),
                "",
            )
        )
    except ValueError:
        return None


def resolve_composio_api_key(settings: Settings) -> str | None:
    """Return the configured Composio API key from file, settings, or environment."""
    for candidate in (
        _read_secret_file(),
        str(getattr(settings, "composio_api_key", "") or "").strip(),
        str(os.getenv("COMPOSIO_API_KEY", "") or "").strip(),
    ):
        if candidate:
            return candidate
    return None


def _read_secret_file() -> str | None:
    path = composio_secrets_path()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _sdk(api_key: str) -> Any:
    try:
        from composio import Composio  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - requirements install the SDK
        raise ComposioConfigurationError(
            "The Composio SDK is not installed in this environment."
        ) from exc
    return Composio(api_key=api_key)


def composio_client_or_none(settings: Settings) -> Any | None:
    """Return an authenticated Composio client, or ``None`` when no key is set.

    Catalog seeding uses this to read the deployment's real toolkit list
    without raising when Composio has not been configured yet.
    """
    api_key = resolve_composio_api_key(settings)
    if not api_key:
        return None
    try:
        return _sdk(api_key)
    except ComposioConfigurationError:
        return None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class ComposioService:
    """Broker Composio toolkits into durable AverQel MCP servers."""

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    @property
    def configured(self) -> bool:
        """Whether a Composio API key is available for broker calls."""
        return resolve_composio_api_key(self.settings) is not None

    def _api_key(self) -> str:
        api_key = resolve_composio_api_key(self.settings)
        if not api_key:
            raise ComposioConfigurationError(
                "Composio API Key is required. Save your key from app.composio.dev "
                "in Settings > Providers > MCP."
            )
        return api_key

    def _client(self) -> Any:
        return _sdk(self._api_key())

    def _session_aad(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, server_id: uuid.UUID
    ) -> bytes:
        return f"composio-session:{tenant_id}:{user_id}:{server_id}".encode()

    def _encrypt_session_id(
        self, session_id: str, *, tenant_id: uuid.UUID, user_id: uuid.UUID, server_id: uuid.UUID
    ) -> dict[str, Any]:
        encrypted = ConnectorSecretCrypto(self.settings).encrypt(
            session_id,
            aad=self._session_aad(tenant_id=tenant_id, user_id=user_id, server_id=server_id),
        )
        return {
            "composio_session_ciphertext": base64.b64encode(encrypted.ciphertext).decode("ascii"),
            "composio_session_nonce": base64.b64encode(encrypted.nonce).decode("ascii"),
            "composio_session_kid": encrypted.kid,
        }

    def _decrypt_session_id(
        self,
        config: dict[str, Any],
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        server_id: uuid.UUID,
    ) -> str | None:
        ciphertext = config.get("composio_session_ciphertext")
        nonce = config.get("composio_session_nonce")
        kid = config.get("composio_session_kid")
        if (
            not isinstance(ciphertext, str)
            or not isinstance(nonce, str)
            or not isinstance(kid, str)
        ):
            return None
        try:
            plaintext = ConnectorSecretCrypto(self.settings).decrypt(
                ciphertext=base64.b64decode(ciphertext),
                nonce=base64.b64decode(nonce),
                kid=kid,
                aad=self._session_aad(tenant_id=tenant_id, user_id=user_id, server_id=server_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unable to decrypt Composio session id: %s", exc)
            return None
        value = plaintext.decode("utf-8").strip()
        return value or None

    # ------------------------------------------------------------------
    # Toolkit catalog
    # ------------------------------------------------------------------

    def list_toolkits(self, *, limit: int = 24, cursor: str | None = None) -> dict[str, Any]:
        """Return a page of Composio toolkits available to this deployment."""
        client = self._client()
        parameters: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if cursor:
            parameters["cursor"] = cursor
        try:
            response = client.toolkits.list(**parameters)
        except Exception as exc:  # noqa: BLE001
            raise ComposioConfigurationError("Unable to load Composio apps.") from exc
        items = [_normalize_toolkit(item) for item in (_field(response, "items", []) or [])]
        return {
            "items": [item for item in items if item["slug"]],
            "next_cursor": _field(response, "next_cursor"),
            "total_pages": _field(response, "total_pages"),
            "current_page": _field(response, "current_page"),
        }

    def toolkit_details(self, toolkit_slug: str) -> dict[str, Any]:
        """Return normalized metadata for one Composio toolkit."""
        toolkit_slug = self._normalize_slug(toolkit_slug)
        client = self._client()
        try:
            details = _normalize_toolkit(client.toolkits.get(toolkit_slug))
        except Exception as exc:  # noqa: BLE001
            raise ComposioConfigurationError("Unable to load this Composio app.") from exc
        if not details["slug"] or not details["name"]:
            raise ComposioConfigurationError("Composio did not return this app's metadata.")
        return details

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def start_connection(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        toolkit_slug: str,
        callback_url: str | None,
    ) -> ComposioConnectionStart:
        """Create or reuse the user's Composio session and authorize one toolkit."""
        toolkit_slug = self._normalize_slug(toolkit_slug)
        details = self.toolkit_details(toolkit_slug)
        authorization_required = not details["no_auth"]
        if authorization_required:
            self._require_callback_url(callback_url, toolkit_slug)

        provider_slug = composio_provider_slug(toolkit_slug)
        set_db_tenant_context(self.db, tenant_id)
        server = self._find_server(
            tenant_id=tenant_id, user_id=user_id, provider_slug=provider_slug
        )
        if server is not None and server.status == "connected" and server.enabled:
            return ComposioConnectionStart(
                server=server, authorization_url=None, already_connected=True
            )

        client = self._client()
        try:
            saved_session_id = (
                self._decrypt_session_id(
                    server.config if isinstance(server.config, dict) else {},
                    tenant_id=tenant_id,
                    user_id=user_id,
                    server_id=server.id,
                )
                if server is not None
                else None
            )
            if saved_session_id:
                session = client.use(saved_session_id, mcp=True)
            else:
                from composio import SESSION_PRESET_DIRECT_TOOLS  # type: ignore[import-untyped]

                session = client.sessions.create(
                    user_id=composio_user_id(tenant_id, user_id),
                    toolkits=[toolkit_slug],
                    session_preset=SESSION_PRESET_DIRECT_TOOLS,
                    mcp=True,
                )
            authorization_url = None
            pending_account_id = None
            if authorization_required:
                request = session.authorize(toolkit_slug, callback_url=callback_url)
                authorization_url = str(_field(request, "redirect_url") or "")
                pending_account_id = str(_field(request, "id", "") or "").strip()
                if not pending_account_id or len(pending_account_id) > 256:
                    raise ComposioConfigurationError(
                        "Composio did not return a valid connection request."
                    )
                self._require_safe_authorization_url(authorization_url)
            session_id = str(session.session_id)
        except ComposioConfigurationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ComposioConfigurationError(
                "Unable to start Composio authorization for this app."
            ) from exc

        if server is None:
            server = MCPServer(
                tenant_id=tenant_id,
                user_id=user_id,
                registry_entry_id=None,
                provider_slug=provider_slug,
                name=details["name"],
                transport="streamable_http",
                config={},
                account_identity={},
                catalog_revision=0,
                enabled=True,
                status="needs_auth",
            )
            self.db.add(server)
            self.db.flush()

        config = dict(server.config or {})
        config.pop("composio_session_ciphertext", None)
        config.update(
            self._encrypt_session_id(
                session_id, tenant_id=tenant_id, user_id=user_id, server_id=server.id
            )
        )
        config.update(
            {
                "composio_toolkit_slug": toolkit_slug,
                "source": "composio",
                "categories": details["categories"],
            }
        )
        if pending_account_id:
            # Bind the redirect to the exact owner-scoped request created above.
            # A callback query value alone is never accepted as proof of ownership.
            config["composio_pending_connected_account_id"] = pending_account_id
        else:
            config.pop("composio_pending_connected_account_id", None)
        server.name = details["name"]
        server.transport = "streamable_http"
        server.enabled = True
        server.status = "needs_auth" if authorization_required else "connecting"
        server.last_error = None
        server.account_identity = {}
        server.config = config
        self.db.commit()
        set_db_tenant_context(self.db, tenant_id)
        self.db.refresh(server)
        MCPEventsRepository(self.db).append(
            tenant_id=tenant_id,
            server_id=server.id,
            user_id=user_id,
            event_type="composio_authorization_started",
            payload={"toolkit": toolkit_slug, "authorization_required": authorization_required},
        )
        self.db.commit()
        return ComposioConnectionStart(
            server=server,
            authorization_url=authorization_url,
            authorization_required=authorization_required,
        )

    def complete_auth(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        toolkit_slug: str,
        connected_account_id: str,
        status: str,
    ) -> MCPServer:
        """Verify a Composio redirect against its owner-scoped pending request."""
        toolkit_slug = self._normalize_slug(toolkit_slug)
        account_id = connected_account_id.strip()
        if not account_id or len(account_id) > 256 or any(ord(char) < 33 for char in account_id):
            raise ComposioConfigurationError("The Composio authorization response is invalid.")
        if status not in {"success", "failed"}:
            raise ComposioConfigurationError("The Composio authorization response is invalid.")
        set_db_tenant_context(self.db, tenant_id)
        server = self._find_server(
            tenant_id=tenant_id,
            user_id=user_id,
            provider_slug=composio_provider_slug(toolkit_slug),
            status="needs_auth",
        )
        if server is None:
            raise ComposioConfigurationError("No matching Composio app connection was started.")
        config = dict(server.config or {})
        if (
            config.get("composio_pending_connected_account_id") != account_id
            or config.get("composio_toolkit_slug") != toolkit_slug
            or not config.get("composio_session_ciphertext")
            or not self._decrypt_session_id(
                config, tenant_id=tenant_id, user_id=user_id, server_id=server.id
            )
        ):
            raise ComposioConfigurationError("The Composio session does not match this app.")

        if status == "failed":
            config.pop("composio_pending_connected_account_id", None)
            server.config = config
            server.status = "disconnected"
            server.enabled = False
            server.last_error = None
            self.db.commit()
            set_db_tenant_context(self.db, tenant_id)
            self.db.refresh(server)
            return server

        expected_user_id = composio_user_id(tenant_id, user_id)
        client = self._client()
        try:
            account = client.connected_accounts.wait_for_connection(account_id, timeout=5)
        except Exception as exc:  # noqa: BLE001
            raise ComposioConfigurationError(
                "Composio could not verify this connection. Return to the same signed-in "
                "AverQel account and retry."
            ) from exc
        returned_account_id = str(_field(account, "id", "") or "").strip()
        returned_toolkit = str(_field(_field(account, "toolkit"), "slug", "") or "").strip().lower()
        returned_user_id = str(_field(account, "user_id", "") or "").strip()
        if (
            returned_account_id != account_id
            or returned_toolkit != toolkit_slug
            or str(_field(account, "status", "")).upper() != "ACTIVE"
            or (returned_user_id and not hmac.compare_digest(returned_user_id, expected_user_id))
        ):
            raise ComposioConfigurationError(
                "The Composio account does not match this AverQel user and app."
            )

        server.account_identity = {"account_id": account_id, "identity_source": "composio"}
        config["composio_connected_account_id"] = account_id
        config.pop("composio_pending_connected_account_id", None)
        server.config = config
        server.status = "connecting"
        server.last_error = None
        self.db.commit()
        set_db_tenant_context(self.db, tenant_id)
        self.db.refresh(server)
        return server

    def disconnect(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, server: MCPServer) -> None:
        """Drop the stored Composio session for one app without touching other apps."""
        config = dict(server.config or {})
        session_id = self._decrypt_session_id(
            config, tenant_id=tenant_id, user_id=user_id, server_id=server.id
        )
        if session_id:
            try:
                self._client().sessions.delete(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.info("Composio session %s was not deleted remotely: %s", session_id, exc)
        for key in (
            "composio_session_ciphertext",
            "composio_session_nonce",
            "composio_session_kid",
            "composio_pending_connected_account_id",
            "composio_connected_account_id",
        ):
            config.pop(key, None)
        server.config = config
        server.status = "disconnected"
        server.enabled = False
        server.last_error = None
        server.account_identity = {}
        self.db.commit()
        set_db_tenant_context(self.db, tenant_id)
        self.db.refresh(server)
        MCPEventsRepository(self.db).append(
            tenant_id=tenant_id,
            server_id=server.id,
            user_id=user_id,
            event_type="composio_authorization_revoked",
            payload={"toolkit": config.get("composio_toolkit_slug")},
        )
        self.db.commit()

    # ------------------------------------------------------------------
    # Endpoint resolution
    # ------------------------------------------------------------------

    def mcp_endpoint_config(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, server: MCPServer
    ) -> dict[str, Any]:
        """Resolve the live Composio MCP endpoint for one durable server.

        The endpoint URL and authorization headers live only for the lifetime
        of the returned runtime; they are never written back to the server row.
        """
        config = server.config if isinstance(server.config, dict) else {}
        toolkit_slug = str(config.get("composio_toolkit_slug") or "").strip().lower()
        if not toolkit_slug or server.provider_slug != composio_provider_slug(toolkit_slug):
            raise ComposioConfigurationError("The Composio MCP session is unavailable.")
        session_id = self._decrypt_session_id(
            config, tenant_id=tenant_id, user_id=user_id, server_id=server.id
        )
        if not session_id:
            raise ComposioConfigurationError("The Composio MCP session is unavailable.")
        try:
            session = self._client().use(session_id, mcp=True)
            endpoint = str(session.mcp.url)
            headers = session.mcp.headers
        except ComposioConfigurationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ComposioConfigurationError(
                "The Composio MCP session could not be loaded."
            ) from exc
        try:
            endpoint = validate_remote_endpoint(endpoint)
        except Exception as exc:  # noqa: BLE001
            raise ComposioConfigurationError("Composio returned an unsafe MCP endpoint.") from exc
        host = (urlsplit(endpoint).hostname or "").lower()
        if host != COMPOSIO_API_HOST:
            raise ComposioConfigurationError("Composio returned an unrecognized MCP endpoint.")
        if not isinstance(headers, dict) or not headers:
            raise ComposioConfigurationError("Composio did not provide MCP session authorization.")
        safe_headers: dict[str, str] = {}
        for key, value in headers.items():
            normalized = str(key).lower()
            if normalized not in _COMPOSIO_RETURN_HEADER_ALLOWLIST:
                raise ComposioConfigurationError("Composio returned an unsupported MCP header.")
            if isinstance(value, str) and value:
                safe_headers[normalized] = value
        if not safe_headers:
            raise ComposioConfigurationError("Composio did not provide MCP session authorization.")
        return {
            "auth_mode": "mcp",
            "credentials": {
                "server_url": endpoint,
                "transport": "streamable_http",
                "oauth_mode": "none",
                "headers": safe_headers,
            },
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_server(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        provider_slug: str,
        status: str | None = None,
    ) -> MCPServer | None:
        statement = select(MCPServer).where(
            MCPServer.tenant_id == tenant_id,
            MCPServer.user_id == user_id,
            MCPServer.provider_slug == provider_slug,
        )
        if status is not None:
            statement = statement.where(MCPServer.status == status)
        return self.db.execute(statement).scalar_one_or_none()

    @staticmethod
    def _normalize_slug(slug: str) -> str:
        """Return the canonical toolkit slug, rejecting anything Composio cannot serve."""
        value = str(slug or "").strip().lower()
        if not COMPOSIO_TOOLKIT_SLUG_PATTERN.fullmatch(value):
            raise ComposioConfigurationError("The Composio app identifier is invalid.")
        return value

    @staticmethod
    def _require_callback_url(callback_url: str | None, toolkit_slug: str) -> None:
        parsed = urlsplit(callback_url or "")
        hostname = (parsed.hostname or "").lower()
        local_http = parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
        callback_toolkits = parse_qs(parsed.query).get("toolkit_slug", [])
        if (
            (parsed.scheme != "https" and not local_http)
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.port == 0
            or parsed.path != COMPOSIO_CALLBACK_PATH
            or callback_toolkits != [toolkit_slug]
        ):
            raise ComposioConfigurationError(
                "Set AverQel's public URL before connecting this Composio app."
            )

    @staticmethod
    def _require_safe_authorization_url(authorization_url: str) -> None:
        parsed = urlsplit(authorization_url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not (host == "composio.dev" or host.endswith(".composio.dev"))
        ):
            raise ComposioConfigurationError("Composio returned an unsafe authorization link.")


def _as_dict(value: Any) -> dict[str, Any]:
    """Best-effort conversion of SDK models to plain dictionaries."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {k: v for k, v in value.__dict__.items() if not k.startswith("_")}
    return {}


def _normalize_toolkit(item: Any) -> dict[str, Any]:
    """Reduce a Composio toolkit payload to the fields AverQel renders.

    ``toolkits.list`` and ``toolkits.get`` return different payloads: only the
    list response carries ``no_auth`` and ``auth_schemes``, so the retrieval
    path derives the same facts from the Composio-managed auth schemes.
    """
    meta = _as_dict(_field(item, "meta", {}) or {})
    auth_schemes = [str(scheme).upper() for scheme in (_field(item, "auth_schemes", []) or [])]
    managed_schemes = [
        str(scheme).upper() for scheme in (_field(item, "composio_managed_auth_schemes", []) or [])
    ]
    declared_no_auth = _field(item, "no_auth", None)
    no_auth = (
        bool(declared_no_auth)
        if declared_no_auth is not None
        else "NO_AUTH" in auth_schemes or not (auth_schemes or managed_schemes)
    )
    return {
        "slug": str(_field(item, "slug", "") or "").strip().lower(),
        "name": str(_field(item, "name", "") or "").strip(),
        "description": str(meta.get("description", "") or "").strip(),
        "logo": str(meta.get("logo", "") or "").strip() or None,
        "app_url": str(meta.get("app_url", "") or "").strip() or None,
        "categories": _category_names(meta.get("categories")),
        "tools_count": int(meta.get("tools_count") or 0),
        "triggers_count": int(meta.get("triggers_count") or 0),
        "no_auth": no_auth,
        "auth_schemes": auth_schemes or managed_schemes,
    }


def _category_names(raw: Any) -> list[str]:
    """Return display names for a Composio toolkit category list.

    Composio returns categories as objects in both endpoints (``{"id",
    "name"}`` when listed, ``{"name", "slug"}`` when retrieved).
    """
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            value = str(entry.get("name") or entry.get("slug") or entry.get("id") or "").strip()
        else:
            value = str(entry or "").strip()
        if value and value not in names:
            names.append(value)
    return names
