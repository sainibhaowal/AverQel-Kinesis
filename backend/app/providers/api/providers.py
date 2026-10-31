from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.auth.tenancy import require_request_tenant_id
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.platform.database.session import get_db, managed_db_session
from app.providers.models.provider_config import ProviderConfig
from app.providers.repositories.provider_assignments import ProviderAssignmentsRepository
from app.providers.schemas import (
    MaskedProviderSecretResponse,
    ProviderAssignmentCreateRequest,
    ProviderAssignmentListResponse,
    ProviderAssignmentResponse,
    ProviderAssignmentUpdateRequest,
    ProviderCatalogEntry,
    ProviderCatalogResponse,
    ProviderConfigCreateRequest,
    ProviderConfigListResponse,
    ProviderConfigResponse,
    ProviderConfigUpdateRequest,
    ProviderDeleteResponse,
    ProviderDisconnectResponse,
    ProviderHealthResponse,
    ProviderModelListResponse,
    ProviderModelPreviewRequest,
    ProviderModelPullRequest,
    ProviderModelPullResponse,
    ProviderModelResponse,
    ProviderOAuthCallbackResponse,
    ProviderOAuthStartRequest,
    ProviderOAuthStartResponse,
    ProviderOAuthStatusResponse,
    ProviderRotateSecretRequest,
    ProviderTestResponse,
)
from app.providers.services import (
    ProviderHealthService,
    ProviderManagementService,
    ProviderModelsService,
    ProviderOAuthService,
)
from app.providers.services.base import ProviderRequestError
from app.providers.services.context_window import resolve_verified_context_window
from app.providers.services.openai_compatible_config import (
    openai_compatible_settings,
    provider_model_listing_enabled,
)
from app.providers.services.provider_models_service import (
    acquire_model_discovery_slot,
    provider_request_error_to_api_error,
    release_model_discovery_slot,
)
from app.providers.services.reasoning_capabilities import merge_reasoning_capabilities
from app.providers.services.registry import ProviderRegistry
from app.system.services.user_notifications import add_user_notification, notify_platform_admins

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])
OPTIONAL_WORKSPACE_QUERY = Query(default=None)
OPTIONAL_CODE_QUERY = Query(default=None)
OPTIONAL_STATE_QUERY = Query(default=None)
OPTIONAL_TENANT_QUERY = Query(default=None)


def _local_model_assets_present(path: Any) -> bool:
    """Return true only when a downloaded local model has usable config and weights."""
    if not path.is_dir() or not (path / "config.json").is_file():
        return False
    return any(
        (path / filename).is_file()
        for filename in ("model.safetensors", "pytorch_model.bin", "model.onnx")
    )


@dataclass(frozen=True, slots=True)
class _ProviderModelContext:
    id: uuid.UUID
    provider_type: str
    api_base_url: str | None
    metadata_json: dict[str, Any]
    supports_chat: bool
    default_chat_model: str | None


def _is_database_lock_timeout(exc: OperationalError) -> bool:
    """Return whether a provider-list failure was caused by a transient lock."""

    original = getattr(exc, "orig", None)
    if getattr(original, "pgcode", None) == "55P03":
        return True
    detail = str(original or exc).lower()
    return (
        "lock timeout" in detail or "locknotavailable" in detail or "database is locked" in detail
    )


def _enforce_tenant_scope(request_tenant_id: uuid.UUID, auth: AuthContext) -> None:
    if request_tenant_id != auth.tenant_id:
        raise ApiError(
            code="TENANT_SCOPE_MISMATCH",
            message="Requested tenant does not match authenticated tenant scope.",
            status_code=403,
        )


def _health_response(row: Any) -> ProviderHealthResponse | None:
    if row is None:
        return None
    return ProviderHealthResponse(
        status=row.status,
        latency_ms=row.latency_ms,
        http_status=row.http_status,
        error_code=row.error_code,
        error_message_redacted=row.error_message_redacted,
        metadata_json=dict(row.metadata_json or {}),
        checked_at=row.checked_at,
    )


def _masked_secret_response(row: Any) -> MaskedProviderSecretResponse:
    return MaskedProviderSecretResponse(
        secret_type=row.secret_type,
        masked_value=row.masked_value,
        expires_at=row.expires_at,
        metadata=dict(row.metadata or {}),
    )


def _provider_config_response(
    service: ProviderManagementService,
    *,
    tenant_id: uuid.UUID,
    provider: Any,
) -> ProviderConfigResponse:
    supports_web_search = provider.provider_type in {"tavily", "searxng"} or bool(
        dict(provider.metadata_json or {}).get("supports_web_search")
    )
    return ProviderConfigResponse(
        id=provider.id,
        tenant_id=provider.tenant_id,
        workspace_id=provider.workspace_id,
        owner_user_id=provider.owner_user_id,
        visibility_scope=provider.visibility_scope,
        provider_type=provider.provider_type,
        display_name=provider.display_name,
        api_base_url=provider.api_base_url,
        auth_mode=provider.auth_mode,
        enabled=provider.enabled,
        is_local=provider.is_local,
        supports_chat=provider.supports_chat,
        supports_embeddings=provider.supports_embeddings,
        supports_reranking=provider.supports_reranking,
        supports_web_search=supports_web_search,
        supports_model_listing=provider.supports_model_listing,
        supports_model_install=provider.supports_model_install,
        default_chat_model=provider.default_chat_model,
        default_embedding_model=provider.default_embedding_model,
        default_reranker_model=provider.default_reranker_model,
        timeout_seconds=provider.timeout_seconds,
        priority=provider.priority,
        metadata_json=dict(provider.metadata_json or {}),
        created_at=provider.created_at,
        updated_at=provider.updated_at,
        secrets=[
            _masked_secret_response(item)
            for item in service.list_masked_secrets(
                tenant_id=tenant_id,
                provider_config_id=provider.id,
            )
        ],
        latest_health=_health_response(
            service.get_latest_health(
                tenant_id=tenant_id,
                provider_config_id=provider.id,
            )
        ),
    )


def _assignment_response(row: Any) -> ProviderAssignmentResponse:
    return ProviderAssignmentResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        workspace_id=row.workspace_id,
        owner_user_id=row.owner_user_id,
        visibility_scope=row.visibility_scope,
        feature_scope=row.feature_scope,
        provider_config_id=row.provider_config_id,
        model_name=row.model_name,
        enabled=row.enabled,
        priority=row.priority,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _custom_reasoning_capabilities(
    provider: ProviderConfig | _ProviderModelContext | None,
) -> dict[str, Any] | None:
    if provider is None or provider.provider_type != "custom":
        return None
    configured = openai_compatible_settings(provider.metadata_json).reasoning
    efforts = (
        [effort for effort in configured.supported_efforts if effort in configured.effort_values]
        if configured.effort_parameter
        else []
    )
    return {
        "supports_reasoning": bool(efforts or configured.toggle_parameter),
        "reasoning_capabilities_known": True,
        "reasoning_visibility": "provider_exposed" if efforts else "hidden",
        "supports_thinking_summary_stream": bool(efforts) and configured.expose_response_reasoning,
        "supported_reasoning_efforts": efforts,
        "supports_thinking_toggle": bool(
            configured.toggle_parameter or configured.effort_off_value is not None
        ),
        "request_controls_on": (
            [configured.effort_parameter] if configured.effort_parameter else []
        )
        + ([configured.toggle_parameter] if configured.toggle_parameter else []),
        "request_controls_off": (
            [configured.toggle_parameter] if configured.toggle_parameter else []
        )
        + (
            [configured.effort_parameter]
            if configured.effort_off_value is not None and configured.effort_parameter
            else []
        ),
        "reasoning_effort_values": {effort: configured.effort_values[effort] for effort in efforts},
    }


def _model_response(
    row: Any,
    *,
    provider_type: str | None = None,
    base_url: str | None = None,
    custom_reasoning: dict[str, Any] | None = None,
) -> ProviderModelResponse:
    advertised_capabilities = dict(row.capabilities_json or {})
    capabilities = dict(advertised_capabilities)
    # Older cache rows can predate reasoning profiles. Resolve those profiles
    # in the response without rewriting persisted user/provider metadata; a
    # successful model refresh stores the merged result for subsequent reads.
    capabilities.update(
        merge_reasoning_capabilities(
            provider_type,
            row.model_name,
            base_url=base_url,
            advertised=capabilities,
        )
    )
    if custom_reasoning and advertised_capabilities.get("reasoning_capabilities_known") is not True:
        capabilities.update(custom_reasoning)
    context_window = row.context_window
    context_source = capabilities.get("context_window_source")
    if not isinstance(context_window, int) or context_window <= 0:
        verified = resolve_verified_context_window(
            row.model_name,
            provider_type=provider_type,
        )
        context_window = verified.context_window
        context_source = context_source or verified.source
    if context_source:
        capabilities["context_window_source"] = context_source
    return ProviderModelResponse(
        id=getattr(row, "id", None),
        provider_config_id=getattr(row, "provider_config_id", None),
        model_name=row.model_name,
        model_kind=row.model_kind,
        display_name=row.display_name,
        context_window=context_window,
        capabilities_json=capabilities,
        is_available=row.is_available,
        last_seen_at=row.last_seen_at,
    )


def _provider_model_responses(
    rows: list[Any],
    *,
    provider: ProviderConfig | _ProviderModelContext | None,
    assigned_chat_model: str | None = None,
) -> list[ProviderModelResponse]:
    provider_type = provider.provider_type if provider is not None else None
    custom_reasoning = _custom_reasoning_capabilities(provider)
    responses = [
        _model_response(
            row,
            provider_type=provider_type,
            base_url=provider.api_base_url if provider is not None else None,
            custom_reasoning=custom_reasoning,
        )
        for row in rows
    ]
    default_model = (
        provider.default_chat_model.strip()
        if provider is not None and provider.supports_chat and provider.default_chat_model
        else ""
    )
    candidate_models = list(
        dict.fromkeys(model for model in (default_model, assigned_chat_model) if model)
    )
    for model_name in candidate_models:
        if any(
            response.model_kind == "chat" and response.model_name == model_name
            for response in responses
        ):
            continue

        # A default or assigned model can be valid before its first successful
        # catalog refresh (including when model listing is disabled). Return its
        # verified profile so DeepSpace can render controls on first use.
        verified = resolve_verified_context_window(
            model_name,
            provider_type=provider_type,
        )
        capabilities = merge_reasoning_capabilities(
            provider_type,
            model_name,
            base_url=provider.api_base_url if provider is not None else None,
        )
        if custom_reasoning is not None:
            capabilities = custom_reasoning
        responses.append(
            ProviderModelResponse(
                id=None,
                provider_config_id=provider.id if provider is not None else None,
                model_name=model_name,
                model_kind="chat",
                display_name=model_name,
                context_window=verified.context_window,
                capabilities_json={
                    **capabilities,
                    **({"context_window_source": verified.source} if verified.source else {}),
                },
                is_available=True,
                last_seen_at=None,
            )
        )
    return responses


def _active_chat_model_for_provider(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    provider_config_id: uuid.UUID,
) -> str | None:
    """Return this user's active chat model for the provider, within tenant scope."""
    assignment = ProviderAssignmentsRepository(db).get_active_assignment(
        tenant_id=tenant_id,
        workspace_id=None,
        feature_scope="chat",
        owner_user_id=user_id,
    )
    if assignment is None or assignment.provider_config_id != provider_config_id:
        return None
    model_name = assignment.model_name
    return model_name.strip() or None if isinstance(model_name, str) else None


def _commit_or_rollback(
    *,
    db: Session,
    message: str,
    context: dict[str, str],
) -> None:
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(message, extra=context, exc_info=True)
        raise ApiError(
            code="INTERNAL_SERVER_ERROR",
            message="Provider operation failed.",
            status_code=500,
        ) from exc


@router.get(
    "",
    response_model=ProviderConfigListResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
async def list_providers(
    workspace_id: uuid.UUID | None = OPTIONAL_WORKSPACE_QUERY,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
) -> ProviderConfigListResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    from app.deepspace.integrations.client_proxy import (
        INTERACTIVE_STORAGE_RPC_TIMEOUT_SECONDS,
        client_proxy_registry,
    )

    if client_proxy_registry.is_client_connected(str(auth.tenant_id), str(auth.user_id)):
        try:
            items_data = await client_proxy_registry.db_proxy_call(
                str(auth.tenant_id),
                str(auth.user_id),
                "db.providers.list_providers",
                {"workspace_id": str(workspace_id) if workspace_id else None},
                timeout=INTERACTIVE_STORAGE_RPC_TIMEOUT_SECONDS,
            )
            return ProviderConfigListResponse(
                items=[ProviderConfigResponse.model_validate(item) for item in items_data]
            )
        except (TimeoutError, RuntimeError, OSError):
            logger.warning(
                "Client provider storage unavailable; using server-side provider data.",
                extra={"tenant_id": str(auth.tenant_id), "user_id": str(auth.user_id)},
            )

    # Do not acquire a database connection until the client-owned storage
    # branch is complete.  A suspended client must never occupy a pool slot
    # while its RPC timeout elapses.
    with managed_db_session() as db:
        service = ProviderManagementService(db)
        try:
            items = service.list_providers(
                tenant_id=auth.tenant_id,
                actor_user_id=auth.user_id,
                workspace_id=workspace_id,
            )
        except OperationalError as exc:
            # A concurrent maintenance request can still hold a row lock if
            # it started before advisory locking was deployed.  Serve the
            # persisted provider configuration instead of surfacing a 500;
            # the next request will refresh the managed cache once the lock is
            # free.  Never hide unrelated database failures.
            if not _is_database_lock_timeout(exc):
                raise
            db.rollback()
            logger.warning(
                "Provider maintenance was lock-contended; serving persisted provider rows.",
                extra={"tenant_id": str(auth.tenant_id), "user_id": str(auth.user_id)},
            )
            items = list(
                service.configs.list_by_workspace(
                    tenant_id=auth.tenant_id,
                    workspace_id=None,
                    owner_user_id=auth.user_id,
                )
            )
        return ProviderConfigListResponse(
            items=[
                _provider_config_response(service, tenant_id=auth.tenant_id, provider=item)
                for item in items
            ]
        )


@router.post(
    "",
    response_model=ProviderConfigResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
async def create_provider(
    payload: ProviderConfigCreateRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderConfigResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    from app.deepspace.integrations.client_proxy import client_proxy_registry

    if client_proxy_registry.is_client_connected(str(auth.tenant_id), str(auth.user_id)):
        payload_dict = payload.model_dump()
        if "workspace_id" in payload_dict and payload_dict["workspace_id"]:
            payload_dict["workspace_id"] = str(payload_dict["workspace_id"])
        provider_data = await client_proxy_registry.db_proxy_call(
            str(auth.tenant_id),
            str(auth.user_id),
            "db.providers.create_provider",
            payload_dict,
        )
        return ProviderConfigResponse.model_validate(provider_data)

    service = ProviderManagementService(db)
    provider = service.create_provider(
        tenant_id=auth.tenant_id,
        workspace_id=payload.workspace_id,
        actor_user_id=auth.user_id,
        provider_type=payload.provider_type,
        display_name=payload.display_name,
        api_base_url=payload.api_base_url,
        auth_mode=payload.auth_mode,
        enabled=payload.enabled,
        is_local=payload.is_local,
        supports_chat=payload.supports_chat,
        supports_embeddings=payload.supports_embeddings,
        supports_reranking=payload.supports_reranking,
        supports_web_search=payload.supports_web_search,
        supports_model_listing=payload.supports_model_listing,
        supports_model_install=payload.supports_model_install,
        default_chat_model=payload.default_chat_model,
        default_embedding_model=payload.default_embedding_model,
        default_reranker_model=payload.default_reranker_model,
        timeout_seconds=payload.timeout_seconds,
        priority=payload.priority,
        metadata_json=payload.metadata_json,
        api_key=payload.api_key,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to create provider config.",
        context={"tenant_id": str(auth.tenant_id), "user_id": str(auth.user_id)},
    )
    return _provider_config_response(service, tenant_id=auth.tenant_id, provider=provider)


@router.get(
    "/catalog/supported-types",
    response_model=ProviderCatalogResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
def get_supported_provider_types(
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderCatalogResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    service = ProviderManagementService(db)
    return ProviderCatalogResponse(
        items=[ProviderCatalogEntry.model_validate(item) for item in service.list_supported_types()]
    )


@router.get(
    "/codex-local/status",
    dependencies=[Depends(require_permissions("providers:read"))],
)
def get_local_codex_status(
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, Any]:
    executable = shutil.which("codex") or "/usr/local/bin/codex"
    if not os.path.exists(executable):
        return {"available": False, "message": "Codex CLI is not installed on this machine."}

    try:
        proc = subprocess.run(
            [executable, "login", "status"], capture_output=True, text=True, timeout=3
        )
        output = proc.stdout + proc.stderr
    except Exception as exc:
        return {"available": False, "message": str(exc)}

    if "Logged in using ChatGPT" not in output:
        return {
            "available": False,
            "message": "Codex CLI is installed but not logged in to ChatGPT. Run 'codex login' in your terminal.",
            "cliMessage": output.strip(),
        }

    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    identity = ""
    auth_file = os.path.join(codex_home, "auth.json")
    if os.path.exists(auth_file):
        try:
            with open(auth_file, encoding="utf-8") as f:
                auth_data = json.load(f)
            id_token = auth_data.get("tokens", {}).get("id_token")
            if id_token:
                parts = id_token.split(".")
                if len(parts) >= 2:
                    padded = parts[1] + "=" * (-len(parts[1]) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(padded))
                    identity = str(claims.get("email") or "")
        except Exception:
            pass

    payload = f"codex-cli\0{os.path.abspath(executable)}\0{os.path.abspath(codex_home)}\0{identity.strip().lower()}"
    account_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    models_file = os.path.join(codex_home, "models_cache.json")
    models: list[dict[str, Any]] = []
    if os.path.exists(models_file):
        try:
            with open(models_file, encoding="utf-8") as f:
                cache_data = json.load(f)
            for m in cache_data.get("models", []):
                slug = m.get("slug") or m.get("id")
                if (
                    slug
                    and not m.get("hidden", False)
                    and slug not in {"gpt-reserve", "codex-auto-review"}
                ):
                    name = m.get("display_name") or m.get("name") or slug
                    models.append(
                        {
                            "id": slug,
                            "name": name,
                            "kind": "chat",
                            "supportedReasoningEfforts": ["low", "medium", "high"],
                        }
                    )
        except Exception:
            pass

    if not models:
        models = [
            {
                "id": "gpt-5.6-sol",
                "name": "GPT-5.6-Sol",
                "kind": "chat",
                "supportedReasoningEfforts": ["low", "medium", "high"],
            },
            {
                "id": "gpt-5.6-terra",
                "name": "GPT-5.6-Terra",
                "kind": "chat",
                "supportedReasoningEfforts": ["low", "medium", "high"],
            },
            {
                "id": "gpt-5.6-luna",
                "name": "GPT-5.6-Luna",
                "kind": "chat",
                "supportedReasoningEfforts": ["low", "medium", "high"],
            },
        ]

    account = {
        "id": account_id,
        "email": identity or None,
        "name": f"Codex CLI ({identity})" if identity else "Codex CLI (ChatGPT)",
        "connected": True,
        "source": "codex_cli",
        "planType": "plus",
    }
    return {
        "available": True,
        "chatgptSignInAvailable": True,
        "accounts": [account],
        "models": models,
        "cliMessage": "Logged in using ChatGPT",
    }


@router.get(
    "/codex-local/models",
    dependencies=[Depends(require_permissions("providers:read"))],
)
def get_local_codex_models(
    auth: AuthContext = Depends(get_auth_context),
) -> list[dict[str, Any]]:
    status = get_local_codex_status(auth=auth)
    return status.get("models", [])


# ---------------------------------------------------------------------------
# Hugging Face Open-Source Embedding Model Discovery & Downloader Hub
# ---------------------------------------------------------------------------


class OpenSourceEmbeddingModelItem(BaseModel):
    model_id: str
    display_name: str
    author: str
    description: str
    dimensions: int
    context_window: int
    size_mb: int
    speed_tier: str
    category: str
    huggingface_url: str
    is_installed: bool
    is_active: bool = False
    local_path: str | None = None


class EmbeddingHubResponse(BaseModel):
    storage_directory: str
    models: list[OpenSourceEmbeddingModelItem]


class InstallEmbeddingModelRequest(BaseModel):
    model_id: str


class InstallEmbeddingModelResponse(BaseModel):
    status: str
    message: str
    model: OpenSourceEmbeddingModelItem


class UninstallEmbeddingModelRequest(BaseModel):
    model_id: str


_CURATED_HF_EMBEDDING_MODELS: list[dict[str, Any]] = [
    {
        "model_id": "BAAI/bge-small-en-v1.5",
        "display_name": "BGE Small English v1.5",
        "author": "BAAI",
        "description": "High-efficiency state-of-the-art embedding model for English retrieval. Very fast on CPU with superior retrieval accuracy.",
        "dimensions": 384,
        "context_window": 512,
        "size_mb": 133,
        "speed_tier": "ultra_fast",
        "category": "lightweight",
        "huggingface_url": "https://huggingface.co/BAAI/bge-small-en-v1.5",
    },
    {
        "model_id": "sentence-transformers/all-MiniLM-L6-v2",
        "display_name": "All MiniLM L6 v2",
        "author": "Sentence Transformers",
        "description": "Industry-standard ultra-compact semantic model. Fastest inference with 5x speed and tiny 90MB memory footprint.",
        "dimensions": 384,
        "context_window": 256,
        "size_mb": 90,
        "speed_tier": "ultra_fast",
        "category": "lightweight",
        "huggingface_url": "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
    },
    {
        "model_id": "intfloat/multilingual-e5-small",
        "display_name": "Multilingual E5 Small",
        "author": "Microsoft / intfloat",
        "description": "Trained across 100+ languages with outstanding multilingual zero-shot similarity and cross-lingual search performance.",
        "dimensions": 384,
        "context_window": 512,
        "size_mb": 470,
        "speed_tier": "fast",
        "category": "multilingual",
        "huggingface_url": "https://huggingface.co/intfloat/multilingual-e5-small",
    },
    {
        "model_id": "BAAI/bge-base-en-v1.5",
        "display_name": "BGE Base English v1.5",
        "author": "BAAI",
        "description": "Leaderboard champion for high-precision semantic search and dense retrieval across complex knowledge bases.",
        "dimensions": 768,
        "context_window": 512,
        "size_mb": 438,
        "speed_tier": "accurate",
        "category": "sota",
        "huggingface_url": "https://huggingface.co/BAAI/bge-base-en-v1.5",
    },
    {
        "model_id": "nomic-ai/nomic-embed-text-v1.5",
        "display_name": "Nomic Embed Text v1.5",
        "author": "Nomic AI",
        "description": "Extended 8192 token context window with Matryoshka variable embedding dimensions. Excellent for long documents and code.",
        "dimensions": 768,
        "context_window": 8192,
        "size_mb": 548,
        "speed_tier": "accurate",
        "category": "long_context",
        "huggingface_url": "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5",
    },
    {
        "model_id": "mixedbread-ai/mxbai-embed-large",
        "display_name": "mxbai Embed Large",
        "author": "Mixedbread AI",
        "description": "High-capacity dense retrieval model trained on over 680M high-quality pairs. Top score on MTEB retrieval benchmark.",
        "dimensions": 1024,
        "context_window": 512,
        "size_mb": 1340,
        "speed_tier": "accurate",
        "category": "sota",
        "huggingface_url": "https://huggingface.co/mixedbread-ai/mxbai-embed-large",
    },
    {
        "model_id": "Snowflake/snowflake-arctic-embed-m",
        "display_name": "Snowflake Arctic Embed M",
        "author": "Snowflake",
        "description": "Engineered for enterprise production search. Balanced size-to-accuracy ratio with robust out-of-domain retrieval.",
        "dimensions": 768,
        "context_window": 512,
        "size_mb": 436,
        "speed_tier": "fast",
        "category": "sota",
        "huggingface_url": "https://huggingface.co/Snowflake/snowflake-arctic-embed-m",
    },
    {
        "model_id": "sentence-transformers/paraphrase-MiniLM-L3-v2",
        "display_name": "Paraphrase MiniLM L3 v2",
        "author": "Sentence Transformers",
        "description": "Ultra-lightweight 61MB model with minimal memory consumption. Ideal for background indexing on low-resource machines.",
        "dimensions": 384,
        "context_window": 128,
        "size_mb": 61,
        "speed_tier": "ultra_fast",
        "category": "lightweight",
        "huggingface_url": "https://huggingface.co/sentence-transformers/paraphrase-MiniLM-L3-v2",
    },
    {
        "model_id": "Alibaba-NLP/gte-small",
        "display_name": "GTE Small",
        "author": "Alibaba NLP",
        "description": "General Text Embeddings model with high ranking on MTEB. Compact 134MB footprint with fast CPU inference.",
        "dimensions": 384,
        "context_window": 512,
        "size_mb": 134,
        "speed_tier": "fast",
        "category": "lightweight",
        "huggingface_url": "https://huggingface.co/Alibaba-NLP/gte-small",
    },
    {
        "model_id": "sentence-transformers/all-mpnet-base-2",
        "display_name": "All MPNet Base v2",
        "author": "Sentence Transformers",
        "description": "One of the most widely adopted general-purpose embedding models with consistently high sentence-level semantic fidelity.",
        "dimensions": 768,
        "context_window": 384,
        "size_mb": 438,
        "speed_tier": "accurate",
        "category": "sota",
        "huggingface_url": "https://huggingface.co/sentence-transformers/all-mpnet-base-2",
    },
]


@router.get(
    "/embeddings/hub",
    response_model=EmbeddingHubResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
def get_embedding_hub_models(
    auth: AuthContext = Depends(get_auth_context),
) -> EmbeddingHubResponse:
    from app.core.averqel_home import averqel_models_path
    from app.providers.services.builtin_local_models import get_builtin_embedding_dimension

    models_dir = averqel_models_path()
    result: list[OpenSourceEmbeddingModelItem] = []
    seen_ids: set[str] = set()

    # 1. Process curated HuggingFace models
    for item in _CURATED_HF_EMBEDDING_MODELS:
        m_id = item["model_id"]
        safe_name = m_id.replace("/", "--").replace(":", "--")
        target_dir = models_dir / safe_name
        is_installed = _local_model_assets_present(target_dir)

        result.append(
            OpenSourceEmbeddingModelItem(
                model_id=m_id,
                display_name=item["display_name"],
                author=item["author"],
                description=item["description"],
                dimensions=item["dimensions"],
                context_window=item["context_window"],
                size_mb=item["size_mb"],
                speed_tier=item["speed_tier"],
                category=item["category"],
                huggingface_url=item["huggingface_url"],
                is_installed=is_installed,
                local_path=str(target_dir) if is_installed else None,
            )
        )
        seen_ids.add(m_id)

    # 2. Check for any user-installed custom models in the app-owned model directory.
    if models_dir.exists():
        for sub in models_dir.iterdir():
            if sub.is_dir():
                meta_file = sub / "averqel_model.json"
                if meta_file.exists():
                    try:
                        with open(meta_file, encoding="utf-8") as f:
                            meta_data = json.load(f)
                        m_id = meta_data.get("model_id")
                        if m_id and m_id not in seen_ids:
                            dim = (
                                meta_data.get("dimension")
                                or get_builtin_embedding_dimension(m_id)
                                or 384
                            )
                            author = m_id.split("/")[0] if "/" in m_id else "Community"
                            if "/" in m_id:
                                hf_url = f"https://huggingface.co/{m_id}"
                            else:
                                alias_map = {
                                    "all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
                                    "paraphrase-MiniLM-L3-v2": "sentence-transformers/paraphrase-MiniLM-L3-v2",
                                    "all-mpnet-base-2": "sentence-transformers/all-mpnet-base-2",
                                    "bge-small-en-v1.5": "BAAI/bge-small-en-v1.5",
                                    "bge-base-en-v1.5": "BAAI/bge-base-en-v1.5",
                                }
                                canon = alias_map.get(m_id)
                                hf_url = (
                                    f"https://huggingface.co/{canon}"
                                    if canon
                                    else f"https://huggingface.co/models?search={m_id}"
                                )

                            result.append(
                                OpenSourceEmbeddingModelItem(
                                    model_id=m_id,
                                    display_name=f"{m_id.split('/')[-1]} (Custom Local)",
                                    author=author,
                                    description="Custom open-source embedding model downloaded and managed locally by AverQel",
                                    dimensions=dim,
                                    context_window=512,
                                    size_mb=meta_data.get("size_mb", 350),
                                    speed_tier="fast",
                                    category="custom",
                                    huggingface_url=hf_url,
                                    is_installed=True,
                                    local_path=str(sub),
                                )
                            )
                            seen_ids.add(m_id)
                    except Exception:
                        pass

    return EmbeddingHubResponse(
        storage_directory=str(models_dir),
        models=result,
    )


@router.post(
    "/embeddings/hub/install",
    response_model=InstallEmbeddingModelResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
def install_embedding_hub_model(
    payload: InstallEmbeddingModelRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> InstallEmbeddingModelResponse:
    from app.core.averqel_home import averqel_models_path
    from app.providers.services.builtin_local_models import get_builtin_embedding_dimension

    model_id = payload.model_id.strip()
    if not model_id:
        raise ApiError(code="INVALID_MODEL_ID", message="Model ID cannot be empty", status_code=400)

    models_dir = averqel_models_path()
    safe_name = model_id.replace("/", "--").replace(":", "--")
    target_dir = models_dir / safe_name
    target_dir.mkdir(parents=True, exist_ok=True)

    dimension = get_builtin_embedding_dimension(model_id) or 384
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=model_id,
            local_dir=str(target_dir),
            ignore_patterns=["*.msgpack", "*.h5", "*.ot", "*.tflite", "flax_model*"],
            max_workers=4,
        )
    except Exception as exc:
        logger.error("Failed to download embedding model %s from Hugging Face: %s", model_id, exc)
        raise ApiError(
            code="MODEL_DOWNLOAD_FAILED",
            message=f"Failed to download model '{model_id}' from Hugging Face: {exc}",
            status_code=502,
        ) from exc

    meta_file = target_dir / "averqel_model.json"
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "model_id": model_id,
                "dimension": dimension,
                "installed": True,
                "source": "huggingface",
                "huggingface_url": f"https://huggingface.co/{model_id}",
                "path": str(target_dir),
            },
            f,
            indent=2,
        )

    # Find curated info if available
    curated = next((m for m in _CURATED_HF_EMBEDDING_MODELS if m["model_id"] == model_id), None)
    author = (
        curated["author"]
        if curated
        else (model_id.split("/")[0] if "/" in model_id else "HuggingFace")
    )
    display_name = curated["display_name"] if curated else model_id

    installed_item = OpenSourceEmbeddingModelItem(
        model_id=model_id,
        display_name=display_name,
        author=author,
        description=(
            curated["description"]
            if curated
            else "Downloaded HuggingFace embedding model for local retrieval"
        ),
        dimensions=dimension,
        context_window=curated["context_window"] if curated else 512,
        size_mb=curated["size_mb"] if curated else 350,
        speed_tier=curated["speed_tier"] if curated else "fast",
        category=curated["category"] if curated else "custom",
        huggingface_url=f"https://huggingface.co/{model_id}",
        is_installed=True,
        local_path=str(target_dir),
    )

    return InstallEmbeddingModelResponse(
        status="success",
        message=f"Model {model_id} downloaded and installed into ~/.averqel-harness/models/embeddings/",
        model=installed_item,
    )


@router.delete(
    "/embeddings/hub/uninstall",
    dependencies=[Depends(require_permissions("providers:write"))],
)
def uninstall_embedding_hub_model(
    payload: UninstallEmbeddingModelRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, str]:
    from app.core.averqel_home import averqel_models_path

    model_id = payload.model_id.strip()
    models_dir = averqel_models_path()
    safe_name = model_id.replace("/", "--").replace(":", "--")
    target_dir = models_dir / safe_name

    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)

    return {"status": "success", "message": f"Uninstalled {model_id} from ~/.averqel-harness"}


# ---------------------------------------------------------------------------
# Hugging Face Open-Source Reranker Model Discovery & Downloader Hub
# ---------------------------------------------------------------------------


class OpenSourceRerankerModelItem(BaseModel):
    model_id: str
    display_name: str
    author: str
    description: str
    architecture: str = "cross-encoder"
    context_window: int
    size_mb: int
    speed_tier: str
    category: str
    huggingface_url: str
    is_installed: bool
    is_active: bool = False
    local_path: str | None = None


class RerankerHubResponse(BaseModel):
    storage_directory: str
    models: list[OpenSourceRerankerModelItem]


class InstallRerankerModelRequest(BaseModel):
    model_id: str


class InstallRerankerModelResponse(BaseModel):
    status: str
    message: str
    model: OpenSourceRerankerModelItem


class UninstallRerankerModelRequest(BaseModel):
    model_id: str


_CURATED_HF_RERANKER_MODELS: list[dict[str, Any]] = [
    {
        "model_id": "BAAI/bge-reranker-v2-m3",
        "display_name": "BGE Reranker v2 M3",
        "author": "BAAI",
        "description": "State-of-the-art multilingual cross-encoder reranker supporting 100+ languages and 8192 token inputs. Highest precision on MTEB reranking.",
        "architecture": "cross-encoder",
        "context_window": 8192,
        "size_mb": 568,
        "speed_tier": "accurate",
        "category": "sota",
        "huggingface_url": "https://huggingface.co/BAAI/bge-reranker-v2-m3",
    },
    {
        "model_id": "cross-encoder/ms-marco-MiniLM-L-12-v2",
        "display_name": "MS MARCO MiniLM L12 v2",
        "author": "Sentence Transformers",
        "description": "Standard high-speed neural cross-encoder fine-tuned on MS MARCO passage ranking. Outstanding speed-to-accuracy balance on CPU.",
        "architecture": "cross-encoder",
        "context_window": 512,
        "size_mb": 134,
        "speed_tier": "fast",
        "category": "lightweight",
        "huggingface_url": "https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-12-v2",
    },
    {
        "model_id": "BAAI/bge-reranker-base",
        "display_name": "BGE Reranker Base",
        "author": "BAAI",
        "description": "Balanced density cross-encoder for English semantic reranking. Provides top-tier precision over vector search candidates.",
        "architecture": "cross-encoder",
        "context_window": 512,
        "size_mb": 438,
        "speed_tier": "fast",
        "category": "accurate",
        "huggingface_url": "https://huggingface.co/BAAI/bge-reranker-base",
    },
    {
        "model_id": "BAAI/bge-reranker-large",
        "display_name": "BGE Reranker Large",
        "author": "BAAI",
        "description": "Flagship 560M parameter cross-encoder offering maximum reranking fidelity on complex queries and technical documentation.",
        "architecture": "cross-encoder",
        "context_window": 512,
        "size_mb": 1340,
        "speed_tier": "accurate",
        "category": "sota",
        "huggingface_url": "https://huggingface.co/BAAI/bge-reranker-large",
    },
    {
        "model_id": "jinaai/jina-reranker-v2-base-multilingual",
        "display_name": "Jina Reranker v2 Multilingual",
        "author": "Jina AI",
        "description": "High-throughput multilingual cross-encoder with 1024 token context window. Excellent for international enterprise search.",
        "architecture": "cross-encoder",
        "context_window": 1024,
        "size_mb": 558,
        "speed_tier": "fast",
        "category": "multilingual",
        "huggingface_url": "https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual",
    },
    {
        "model_id": "mixedbread-ai/mxbai-rerank-base-v1",
        "display_name": "mxbai Rerank Base v1",
        "author": "Mixedbread AI",
        "description": "Optimized cross-encoder scoring top results on BEIR benchmark. Superior candidate re-ranking for RAG and agent knowledge.",
        "architecture": "cross-encoder",
        "context_window": 512,
        "size_mb": 438,
        "speed_tier": "accurate",
        "category": "sota",
        "huggingface_url": "https://huggingface.co/mixedbread-ai/mxbai-rerank-base-v1",
    },
    {
        "model_id": "cross-encoder/ms-marco-TinyBERT-L-2-v2",
        "display_name": "MS MARCO TinyBERT L2 v2",
        "author": "Sentence Transformers",
        "description": "Ultra-compact 17MB cross-encoder for edge devices and low-resource background tasks. Lightning-fast evaluation latency.",
        "architecture": "cross-encoder",
        "context_window": 512,
        "size_mb": 17,
        "speed_tier": "ultra_fast",
        "category": "lightweight",
        "huggingface_url": "https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L-2-v2",
    },
]


@router.get(
    "/rerankers/hub",
    response_model=RerankerHubResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
def get_reranker_hub_models(
    auth: AuthContext = Depends(get_auth_context),
) -> RerankerHubResponse:
    from app.core.averqel_home import averqel_rerankers_path

    models_dir = averqel_rerankers_path()
    result: list[OpenSourceRerankerModelItem] = []
    seen_ids: set[str] = set()

    # 1. Process curated HuggingFace reranker models
    for item in _CURATED_HF_RERANKER_MODELS:
        m_id = item["model_id"]
        safe_name = m_id.replace("/", "--").replace(":", "--")
        target_dir = models_dir / safe_name
        is_installed = _local_model_assets_present(target_dir)

        result.append(
            OpenSourceRerankerModelItem(
                model_id=m_id,
                display_name=item["display_name"],
                author=item["author"],
                description=item["description"],
                architecture=item.get("architecture", "cross-encoder"),
                context_window=item["context_window"],
                size_mb=item["size_mb"],
                speed_tier=item["speed_tier"],
                category=item["category"],
                huggingface_url=item["huggingface_url"],
                is_installed=is_installed,
                local_path=str(target_dir) if is_installed else None,
            )
        )
        seen_ids.add(m_id)

    # 2. Check for any user-installed custom rerankers in the app-owned model directory.
    if models_dir.exists():
        for sub in models_dir.iterdir():
            if sub.is_dir():
                meta_file = sub / "averqel_model.json"
                if meta_file.exists():
                    try:
                        with open(meta_file, encoding="utf-8") as f:
                            meta_data = json.load(f)
                        m_id = meta_data.get("model_id")
                        if m_id and m_id not in seen_ids:
                            author = m_id.split("/")[0] if "/" in m_id else "Community"
                            if "/" in m_id:
                                hf_url = f"https://huggingface.co/{m_id}"
                            else:
                                hf_url = f"https://huggingface.co/models?pipeline_tag=text-classification&search={m_id}"

                            result.append(
                                OpenSourceRerankerModelItem(
                                    model_id=m_id,
                                    display_name=f"{m_id.split('/')[-1]} (Custom Local)",
                                    author=author,
                                    description="Custom open-source reranker model downloaded and managed locally by AverQel",
                                    architecture="cross-encoder",
                                    context_window=meta_data.get("context_window", 512),
                                    size_mb=meta_data.get("size_mb", 350),
                                    speed_tier="fast",
                                    category="custom",
                                    huggingface_url=hf_url,
                                    is_installed=True,
                                    local_path=str(sub),
                                )
                            )
                            seen_ids.add(m_id)
                    except Exception:
                        pass

    return RerankerHubResponse(
        storage_directory=str(models_dir),
        models=result,
    )


@router.post(
    "/rerankers/hub/install",
    response_model=InstallRerankerModelResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
def install_reranker_hub_model(
    payload: InstallRerankerModelRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> InstallRerankerModelResponse:
    from app.core.averqel_home import averqel_rerankers_path

    model_id = payload.model_id.strip()
    if not model_id:
        raise ApiError(
            code="INVALID_MODEL_ID",
            message="Model ID cannot be blank.",
            status_code=400,
        )

    models_dir = averqel_rerankers_path()
    safe_name = model_id.replace("/", "--").replace(":", "--")
    target_dir = models_dir / safe_name
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=model_id,
            local_dir=str(target_dir),
            ignore_patterns=["*.msgpack", "*.h5", "*.ot", "flax_model.msgpack", "tf_model.h5"],
        )

        meta_file = target_dir / "averqel_model.json"
        meta_payload = {
            "model_id": model_id,
            "architecture": "cross-encoder",
            "installed": True,
            "source": "huggingface",
            "huggingface_url": f"https://huggingface.co/{model_id}",
            "path": str(target_dir),
            "download_error": None,
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta_payload, f, indent=2)

    except Exception as exc:
        logger.error(
            f"Failed to download reranker model {model_id} from Hugging Face: {exc}", exc_info=True
        )
        raise ApiError(
            code="MODEL_DOWNLOAD_FAILED",
            message=f"Failed to download model '{model_id}' from Hugging Face: {exc}",
            status_code=502,
        ) from exc

    curated = next((m for m in _CURATED_HF_RERANKER_MODELS if m["model_id"] == model_id), None)
    author = (
        curated["author"]
        if curated
        else (model_id.split("/")[0] if "/" in model_id else "HuggingFace")
    )
    display_name = curated["display_name"] if curated else model_id

    installed_item = OpenSourceRerankerModelItem(
        model_id=model_id,
        display_name=display_name,
        author=author,
        description=(
            curated["description"]
            if curated
            else "Downloaded HuggingFace cross-encoder reranker for local retrieval"
        ),
        architecture="cross-encoder",
        context_window=curated["context_window"] if curated else 512,
        size_mb=curated["size_mb"] if curated else 350,
        speed_tier=curated["speed_tier"] if curated else "fast",
        category=curated["category"] if curated else "custom",
        huggingface_url=f"https://huggingface.co/{model_id}",
        is_installed=True,
        local_path=str(target_dir),
    )

    return InstallRerankerModelResponse(
        status="success",
        message=f"Reranker {model_id} downloaded and installed into ~/.averqel-harness/models/rerankers/",
        model=installed_item,
    )


@router.delete(
    "/rerankers/hub/uninstall",
    dependencies=[Depends(require_permissions("providers:write"))],
)
def uninstall_reranker_hub_model(
    payload: UninstallRerankerModelRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, str]:
    from app.core.averqel_home import averqel_rerankers_path

    model_id = payload.model_id.strip()
    models_dir = averqel_rerankers_path()
    safe_name = model_id.replace("/", "--").replace(":", "--")
    target_dir = models_dir / safe_name

    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)

    return {
        "status": "success",
        "message": f"Uninstalled {model_id} from ~/.averqel-harness/models/rerankers",
    }


@router.post(
    "/models/preview",
    response_model=ProviderModelListResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
def preview_models(
    payload: ProviderModelPreviewRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderModelListResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    provider = ProviderConfig(
        tenant_id=auth.tenant_id,
        owner_user_id=auth.user_id,
        workspace_id=payload.workspace_id,
        provider_type=payload.provider_type,
        display_name=payload.provider_type,
        api_base_url=payload.api_base_url,
        auth_mode=payload.auth_mode,
        enabled=True,
        is_local=payload.auth_mode == "local_no_key"
        or payload.provider_type in {"ollama", "lmstudio"},
        supports_chat=payload.supports_chat,
        supports_embeddings=payload.supports_embeddings,
        supports_reranking=payload.supports_reranking,
        supports_model_listing=payload.supports_model_listing,
        supports_model_install=False,
        default_chat_model=None,
        default_embedding_model=None,
        default_reranker_model=None,
        timeout_seconds=30,
        priority=100,
        metadata_json=dict(payload.metadata_json or {}),
    )
    registry = ProviderRegistry(settings)
    acquire_model_discovery_slot("preview")
    try:
        discovery_api_key = (
            None if payload.provider_type.strip().lower() == "cline" else payload.api_key
        )
        discovery = registry.get_model_discovery_provider_from_config(
            provider, api_key=discovery_api_key
        )
        logger.info(
            f"Model discovery for {payload.provider_type}: base_url={getattr(discovery, 'base_url', 'N/A')}"
        )
        listing_available = bool(
            provider_model_listing_enabled(
                payload.provider_type,
                payload.supports_model_listing,
            )
            and getattr(discovery, "supports_model_listing", True)
        )
        infos = list(discovery.list_models()) if listing_available and payload.supports_chat else []
        logger.info(f"Found {len(infos)} chat models, supports_chat={payload.supports_chat}")
        if listing_available and payload.supports_embeddings:
            infos.extend(list(discovery.list_embedding_models()))
        if listing_available and payload.supports_reranking:
            infos.extend(list(discovery.list_reranker_models()))
    except ProviderRequestError as exc:
        logger.error(f"ProviderRequestError: {exc}")
        raise provider_request_error_to_api_error(exc, operation="preview") from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Exception in model preview: {exc}", exc_info=True)
        raise ApiError(
            code="PROVIDER_TEST_FAILED",
            message="Provider model preview failed.",
            status_code=502,
        ) from exc
    finally:
        release_model_discovery_slot()

    items: list[ProviderModelResponse] = []
    seen: set[tuple[str, str]] = set()
    for info in infos:
        key = (info.name, info.kind)
        if key in seen:
            continue
        seen.add(key)
        context_window = info.context_window
        context_source = info.context_window_source
        if not isinstance(context_window, int) or context_window <= 0:
            verified = resolve_verified_context_window(
                info.name,
                provider_type=payload.provider_type,
            )
            context_window = verified.context_window
            context_source = context_source or verified.source
        capabilities = dict(info.capabilities)
        capabilities.update(
            merge_reasoning_capabilities(
                payload.provider_type,
                info.name,
                base_url=payload.api_base_url,
                advertised=capabilities,
            )
        )
        if context_source:
            capabilities["context_window_source"] = context_source
        items.append(
            ProviderModelResponse(
                id=None,
                provider_config_id=None,
                model_name=info.name,
                model_kind=info.kind,
                display_name=info.display_name,
                context_window=context_window,
                capabilities_json=capabilities,
                is_available=True,
                last_seen_at=None,
            )
        )
    return ProviderModelListResponse(items=items)


@router.post(
    "/assignments",
    response_model=ProviderAssignmentResponse,
    dependencies=[Depends(require_permissions("providers:assign"))],
)
def create_assignment(
    payload: ProviderAssignmentCreateRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderAssignmentResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    row = ProviderManagementService(db).create_assignment(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        workspace_id=payload.workspace_id,
        feature_scope=payload.feature_scope,
        provider_config_id=payload.provider_config_id,
        model_name=payload.model_name,
        enabled=payload.enabled,
        priority=payload.priority,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to create provider assignment.",
        context={
            "tenant_id": str(auth.tenant_id),
            "provider_id": str(payload.provider_config_id),
        },
    )
    return _assignment_response(row)


@router.get(
    "/assignments",
    response_model=ProviderAssignmentListResponse,
    dependencies=[Depends(require_permissions("providers:assign"))],
)
def list_assignments(
    workspace_id: uuid.UUID | None = OPTIONAL_WORKSPACE_QUERY,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderAssignmentListResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    rows = ProviderManagementService(db).list_assignments(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
        workspace_id=workspace_id,
    )
    return ProviderAssignmentListResponse(items=[_assignment_response(row) for row in rows])


@router.patch(
    "/assignments/{assignment_id}",
    response_model=ProviderAssignmentResponse,
    dependencies=[Depends(require_permissions("providers:assign"))],
)
def update_assignment(
    assignment_id: uuid.UUID,
    payload: ProviderAssignmentUpdateRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderAssignmentResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    values = payload.model_dump(exclude_none=True)
    row = ProviderManagementService(db).update_assignment(
        tenant_id=auth.tenant_id,
        assignment_id=assignment_id,
        actor_user_id=auth.user_id,
        values=values,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to update provider assignment.",
        context={"tenant_id": str(auth.tenant_id), "assignment_id": str(assignment_id)},
    )
    return _assignment_response(row)


@router.delete(
    "/assignments/{assignment_id}",
    status_code=204,
    dependencies=[Depends(require_permissions("providers:assign"))],
)
def delete_assignment(
    assignment_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Response:
    _enforce_tenant_scope(request_tenant_id, auth)
    deleted = ProviderManagementService(db).delete_assignment(
        tenant_id=auth.tenant_id,
        assignment_id=assignment_id,
        actor_user_id=auth.user_id,
    )
    if not deleted:
        raise ApiError(
            code="PROVIDER_ASSIGNMENT_INVALID",
            message="Provider assignment not found.",
            status_code=404,
        )
    _commit_or_rollback(
        db=db,
        message="Failed to delete provider assignment.",
        context={"tenant_id": str(auth.tenant_id), "assignment_id": str(assignment_id)},
    )
    return Response(status_code=204)


@router.get(
    "/{provider_id}",
    response_model=ProviderConfigResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
def get_provider(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderConfigResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    service = ProviderManagementService(db)
    provider = service.get_provider(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    return _provider_config_response(service, tenant_id=auth.tenant_id, provider=provider)


@router.patch(
    "/{provider_id}",
    response_model=ProviderConfigResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
def update_provider(
    provider_id: uuid.UUID,
    payload: ProviderConfigUpdateRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderConfigResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    values = payload.model_dump(exclude_none=True, exclude={"api_key"})
    service = ProviderManagementService(db)
    provider = service.update_provider(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
        values=values,
        api_key=payload.api_key,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to update provider config.",
        context={"tenant_id": str(auth.tenant_id), "provider_id": str(provider_id)},
    )
    return _provider_config_response(service, tenant_id=auth.tenant_id, provider=provider)


@router.delete(
    "/{provider_id}",
    response_model=ProviderDeleteResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
def delete_provider(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderDeleteResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    service = ProviderManagementService(db)
    status = service.delete_or_disable_provider(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to delete or disable provider config.",
        context={"tenant_id": str(auth.tenant_id), "provider_id": str(provider_id)},
    )
    return ProviderDeleteResponse(provider_id=provider_id, status=status)


@router.post(
    "/{provider_id}/test",
    response_model=ProviderTestResponse,
    dependencies=[Depends(require_permissions("providers:test"))],
)
def test_provider(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderTestResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    row = ProviderModelsService(db, ProviderRegistry(settings)).test_provider(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    if row.status.lower() not in {"healthy", "ok", "success"}:
        provider = ProviderManagementService(db).get_provider(
            tenant_id=auth.tenant_id,
            provider_config_id=provider_id,
            actor_user_id=auth.user_id,
        )
        recipient_id = provider.owner_user_id or auth.user_id
        add_user_notification(
            db,
            tenant_id=auth.tenant_id,
            recipient_user_id=recipient_id,
            event_domain="provider",
            event_type="health_check_failed",
            title="Provider health check failed",
            message=f"{provider.display_name} is not responding successfully. Review provider settings and health details.",
            href="/dashboard/settings/providers",
            resource_id=provider_id,
            idempotency_key=f"provider:failed:{provider_id}:{row.checked_at.strftime('%Y%m%d%H%M')}",
        )
        notify_platform_admins(
            db,
            event_domain="provider",
            event_type="health_check_failed",
            title="Provider health check failed",
            message=f"A provider health check failed for {provider.display_name}.",
            href="/dashboard/settings/providers",
            resource_id=provider_id,
        )
    _commit_or_rollback(
        db=db,
        message="Failed to test provider.",
        context={"tenant_id": str(auth.tenant_id), "provider_id": str(provider_id)},
    )
    return ProviderTestResponse(
        provider_id=str(provider_id),
        status=row.status,
        latency_ms=row.latency_ms,
        http_status=row.http_status,
        error_code=row.error_code,
        error_message_redacted=row.error_message_redacted,
        metadata_json=dict(row.metadata_json or {}),
        checked_at=row.checked_at,
    )


@router.get(
    "/{provider_id}/health",
    response_model=ProviderHealthResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
def get_provider_health(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderHealthResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    ProviderManagementService(db).get_provider(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    service = ProviderHealthService(db, ProviderRegistry(settings))
    row = service.latest(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
    )
    if row is None:
        raise ApiError(
            code="PROVIDER_HEALTH_CHECK_FAILED",
            message="Provider health has not been checked yet.",
            status_code=404,
        )
    response = _health_response(row)
    if response is None:
        raise ApiError(
            code="PROVIDER_HEALTH_CHECK_FAILED",
            message="Provider health has not been checked yet.",
            status_code=404,
        )
    return response


@router.post(
    "/{provider_id}/models/refresh",
    response_model=ProviderModelListResponse,
    dependencies=[Depends(require_permissions("providers:test"))],
)
def refresh_provider_models(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderModelListResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    # Keep the provider row loaded before refresh. Its defaults can change while
    # the catalog is refreshed, so snapshot it after refresh but before commit.
    provider_row = db.get(ProviderConfig, provider_id)
    assigned_chat_model = _active_chat_model_for_provider(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        provider_config_id=provider_id,
    )
    models_service = ProviderModelsService(db, ProviderRegistry(settings))
    rows = models_service.refresh_models(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    provider = (
        _ProviderModelContext(
            id=provider_row.id,
            provider_type=provider_row.provider_type,
            api_base_url=provider_row.api_base_url,
            metadata_json=dict(provider_row.metadata_json or {}),
            supports_chat=provider_row.supports_chat,
            default_chat_model=provider_row.default_chat_model,
        )
        if provider_row is not None
        else None
    )
    _commit_or_rollback(
        db=db,
        message="Failed to refresh provider models.",
        context={"tenant_id": str(auth.tenant_id), "provider_id": str(provider_id)},
    )
    return ProviderModelListResponse(
        items=_provider_model_responses(
            rows,
            provider=provider,
            assigned_chat_model=assigned_chat_model,
        )
    )


@router.get(
    "/{provider_id}/models",
    response_model=ProviderModelListResponse,
    dependencies=[Depends(require_permissions("providers:read"))],
)
def list_provider_models(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderModelListResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    models_service = ProviderModelsService(db, ProviderRegistry(settings))
    rows = models_service.list_models(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    provider = db.get(ProviderConfig, provider_id)
    assigned_chat_model = _active_chat_model_for_provider(
        db,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        provider_config_id=provider_id,
    )
    return ProviderModelListResponse(
        items=_provider_model_responses(
            rows,
            provider=provider,
            assigned_chat_model=assigned_chat_model,
        )
    )


@router.post(
    "/{provider_id}/models/pull",
    response_model=ProviderModelPullResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
def pull_provider_model(
    provider_id: uuid.UUID,
    payload: ProviderModelPullRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderModelPullResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    model_name = ProviderModelsService(db, ProviderRegistry(settings)).pull_model(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        model_name=payload.model_name,
        actor_user_id=auth.user_id,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to pull provider model.",
        context={"tenant_id": str(auth.tenant_id), "provider_id": str(provider_id)},
    )
    return ProviderModelPullResponse(
        status="accepted", message=f"Model {model_name} pull requested."
    )


@router.post(
    "/oauth/openai/start",
    response_model=ProviderOAuthStartResponse,
    dependencies=[Depends(require_permissions("providers:oauth"))],
)
def start_openai_oauth(
    payload: ProviderOAuthStartRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderOAuthStartResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    if payload.provider_type != "openai":
        raise ApiError(
            code="PROVIDER_OAUTH_UNSUPPORTED",
            message="OAuth is only defined for OpenAI/Codex in this phase.",
            status_code=400,
        )
    available, authorization_url, message = ProviderOAuthService(db, settings).start(
        tenant_id=auth.tenant_id,
        actor_user_id=auth.user_id,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to initialize provider OAuth flow.",
        context={"tenant_id": str(auth.tenant_id), "provider_type": "openai"},
    )
    return ProviderOAuthStartResponse(
        available=available,
        authorization_url=authorization_url,
        message=message,
    )


@router.get("/oauth/openai/callback", response_model=ProviderOAuthCallbackResponse)
def openai_oauth_callback(
    code: str | None = OPTIONAL_CODE_QUERY,
    state: str | None = OPTIONAL_STATE_QUERY,
    tenant_id: uuid.UUID | None = OPTIONAL_TENANT_QUERY,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderOAuthCallbackResponse:
    if tenant_id is None:
        raise ApiError(
            code="TENANT_REQUIRED",
            message="tenant_id query parameter is required for OAuth callback handling.",
            status_code=400,
        )
    connected, message = ProviderOAuthService(db, settings).callback(
        tenant_id=tenant_id,
        actor_user_id=None,
        code=code,
        state=state,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to handle provider OAuth callback.",
        context={"tenant_id": str(tenant_id), "provider_type": "openai"},
    )
    return ProviderOAuthCallbackResponse(connected=connected, message=message)


@router.get(
    "/oauth/openai/status",
    response_model=ProviderOAuthStatusResponse,
    dependencies=[Depends(require_permissions("providers:oauth"))],
)
def openai_oauth_status(
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ProviderOAuthStatusResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    service = ProviderOAuthService(db, settings)
    available, message = service.status(tenant_id=auth.tenant_id)
    return ProviderOAuthStatusResponse(
        available=available,
        connected=service.connected(tenant_id=auth.tenant_id),
        provider_type="openai",
        message=message,
    )


@router.post(
    "/{provider_id}/disconnect",
    response_model=ProviderDisconnectResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
def disconnect_provider(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderDisconnectResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    count = ProviderManagementService(db).disconnect_provider(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to disconnect provider.",
        context={"tenant_id": str(auth.tenant_id), "provider_id": str(provider_id)},
    )
    return ProviderDisconnectResponse(provider_id=str(provider_id), revoked_secret_count=count)


@router.post(
    "/{provider_id}/refresh-token",
    dependencies=[Depends(require_permissions("providers:oauth"))],
)
def refresh_provider_token(
    provider_id: uuid.UUID,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderDisconnectResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    ProviderManagementService(db).get_provider(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        actor_user_id=auth.user_id,
    )
    raise ApiError(
        code="PROVIDER_OAUTH_UNSUPPORTED",
        message="Provider token refresh is not available until officially supported and implemented.",
        status_code=400,
    )


@router.post(
    "/{provider_id}/rotate-secret",
    response_model=ProviderDisconnectResponse,
    dependencies=[Depends(require_permissions("providers:write"))],
)
def rotate_provider_secret(
    provider_id: uuid.UUID,
    payload: ProviderRotateSecretRequest,
    request_tenant_id: uuid.UUID = Depends(require_request_tenant_id),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> ProviderDisconnectResponse:
    _enforce_tenant_scope(request_tenant_id, auth)
    ProviderManagementService(db).rotate_secret(
        tenant_id=auth.tenant_id,
        provider_config_id=provider_id,
        secret_type=payload.secret_type,
        secret_value=payload.secret_value,
        actor_user_id=auth.user_id,
    )
    _commit_or_rollback(
        db=db,
        message="Failed to rotate provider secret.",
        context={"tenant_id": str(auth.tenant_id), "provider_id": str(provider_id)},
    )
    return ProviderDisconnectResponse(provider_id=str(provider_id), revoked_secret_count=0)
