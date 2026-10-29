"""The model a DeepSpace conversation runs on.

The routes let a user read the conversation's model choice, record a model
of any provider the tenant may use, and let DeepSpace choose the model that
fits the work.  The deployment decides whether DeepSpace may choose; the
conversation's recorded choice decides the model inside that mode.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import Settings, get_settings
from app.deepspace.services.model_selection import (
    ConversationModel,
    ModelSelectionError,
    ModelSelectionMode,
    ModelSelectionService,
)
from app.platform.database.session import get_db
from app.providers.services.selection_service import ProviderSelectionService

router = APIRouter(prefix="/deepspace/chats", tags=["deepspace-model-selection"])


def _assigned_model(
    *,
    db: Session,
    auth: AuthContext,
    settings: Settings,
) -> ConversationModel | None:
    """Return the model the deployment assigned to the caller.

    Args:
        db: The database session.
        auth: The caller's identity.
        settings: The deployment settings.

    Returns:
        The assigned model, or None when the deployment assigned none.
    """
    try:
        selection = ProviderSelectionService(db, settings).resolve_chat(
            tenant_id=auth.tenant_id,
            workspace_id=None,
            actor_user_id=auth.user_id,
            record_audit=False,
        )
    except Exception:  # noqa: BLE001
        return None
    candidate = selection.candidates[0] if selection.candidates else None
    if candidate is None or candidate.provider_config_id is None:
        return None
    return ConversationModel(
        provider_config_id=candidate.provider_config_id,
        model_name=candidate.model_name,
    )


def _conversation_model_state(
    *,
    db: Session,
    auth: AuthContext,
    conversation_id: uuid.UUID,
    settings: Settings,
) -> dict[str, Any]:
    """Describe the model one conversation runs on.

    Args:
        db: The database session.
        auth: The caller's identity.
        conversation_id: The conversation to describe.
        settings: The deployment settings.

    Returns:
        The conversation's model choice.
    """
    service = ModelSelectionService(db)
    recorded = service.recorded_selection(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    mode: ModelSelectionMode = service.effective_mode(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        settings=settings,
    )
    assigned = _assigned_model(db=db, auth=auth, settings=settings)
    effective = recorded if mode == "manual" and recorded is not None else assigned
    return {
        "mode": mode,
        "effective_model": effective.model_name if effective else None,
        "effective_provider_config_id": (str(effective.provider_config_id) if effective else None),
        "recorded_model": recorded.model_name if recorded else None,
        "recorded_provider_config_id": (str(recorded.provider_config_id) if recorded else None),
        "deployment_default": service.deployment_mode(settings),
        "model": recorded.to_dict() if recorded else None,
    }


@router.get(
    "/models",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_chat_models(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List every model the caller's tenant may run a conversation on."""
    models = ModelSelectionService(db).selectable_models(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
    )
    return {"models": models}


@router.get(
    "/{conversation_id}/model",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_model(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the model the conversation runs on."""
    return _conversation_model_state(
        db=db,
        auth=auth,
        conversation_id=conversation_id,
        settings=settings,
    )


@router.put(
    "/{conversation_id}/model",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_conversation_model(
    conversation_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Record the model the conversation runs on."""
    service = ModelSelectionService(db)
    requested_mode = str(payload.get("mode") or "").strip().lower()
    if requested_mode:
        if requested_mode != "auto":
            raise HTTPException(status_code=400, detail="The mode must be 'auto'.")
        if not settings.deepspace_model_auto_choice_enabled:
            raise HTTPException(
                status_code=403,
                detail="This deployment does not let DeepSpace choose the model.",
            )
        service.set_auto_mode(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
        )
        return _conversation_model_state(
            db=db,
            auth=auth,
            conversation_id=conversation_id,
            settings=settings,
        )

    provider_config_id = str(payload.get("provider_config_id") or "").strip()
    model_name = str(payload.get("model_name") or "").strip()
    try:
        parsed_provider = uuid.UUID(provider_config_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="A provider is required.") from exc
    if (
        service.usable_provider(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            provider_config_id=parsed_provider,
        )
        is None
    ):
        raise HTTPException(
            status_code=400,
            detail="This provider cannot run the conversation.",
        )
    try:
        service.set_selection(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            provider_config_id=parsed_provider,
            model_name=model_name,
        )
    except ModelSelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _conversation_model_state(
        db=db,
        auth=auth,
        conversation_id=conversation_id,
        settings=settings,
    )


__all__ = ["router"]
