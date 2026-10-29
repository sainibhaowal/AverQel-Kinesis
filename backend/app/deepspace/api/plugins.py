"""The plugin routes for DeepSpace.

A plugin adds tools, guidance, and tool restrictions to the agent without
changing the product.  The routes here list the plugins the deployment can
offer, and record which of them one conversation runs.  The deployment's
policy always wins: a plugin the deployment did not enable cannot be
switched on from a conversation.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import Settings, get_settings
from app.core.ids import generate_uuid7_with_fallback
from app.deepspace.models.custom_preset import DeepSpaceCustomPreset
from app.deepspace.services.plugins import (
    PluginStateService,
    plugin_registry,
    plugin_runtime_summary,
)
from app.deepspace.services.presets import (
    CUSTOM_PRESET_ID,
    CUSTOM_PRESET_PREFIX,
    DEFAULT_PRESET_ID,
    PresetEnablementService,
    PresetNotFoundError,
    PresetSettingsError,
    PresetStateService,
    PresetUnavailableError,
    custom_preset_from_settings,
    is_custom_preset_id,
    list_presets,
    resolve_preset,
    user_default_conversation_id,
)
from app.platform.database.session import get_db

router = APIRouter(prefix="/deepspace/chats", tags=["deepspace-plugins"])


def _custom_settings_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Read the optional custom-preset settings object from a request."""
    if "settings" not in payload or payload["settings"] is None:
        return None
    raw_settings = payload["settings"]
    if not isinstance(raw_settings, dict):
        raise HTTPException(status_code=400, detail="Preset settings must be an object.")
    return raw_settings


def _custom_id(row: DeepSpaceCustomPreset) -> str:
    """Return the public id for a saved custom preset."""
    return f"{CUSTOM_PRESET_PREFIX}{row.id}"


def _custom_settings(row: DeepSpaceCustomPreset) -> dict[str, Any]:
    """Return validated custom settings stored in a user-owned row."""
    settings = dict(row.settings_json or {})
    settings["id"] = _custom_id(row)
    settings["name"] = row.name
    settings["description"] = row.description
    return settings


def _custom_row(
    db: Session,
    *,
    auth: AuthContext,
    preset_id: str,
) -> DeepSpaceCustomPreset | None:
    """Find one saved custom preset belonging to the authenticated user."""
    normalized = str(preset_id or "").strip().lower()
    if not normalized.startswith(CUSTOM_PRESET_PREFIX):
        return None
    row_id = normalized.removeprefix(CUSTOM_PRESET_PREFIX)
    return db.execute(
        select(DeepSpaceCustomPreset).where(
            DeepSpaceCustomPreset.id == row_id,
            DeepSpaceCustomPreset.tenant_id == str(auth.tenant_id),
            DeepSpaceCustomPreset.user_id == str(auth.user_id),
        )
    ).scalar_one_or_none()


def _custom_preset_payload(
    row: DeepSpaceCustomPreset,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Publish one saved custom preset with effective runtime settings."""
    stored = _custom_settings(row)
    preset = custom_preset_from_settings(stored)
    resolution = resolve_preset(preset.id, settings=settings, custom_settings=stored)
    return {
        "id": preset.id,
        "name": preset.name,
        "description": preset.description,
        "ptc_enabled": resolution.ptc_enabled,
        "plan_mode_by_default": resolution.plan_mode_by_default,
        "editable": True,
        "tools": list(resolution.tool_allowlist or ()),
        "sandbox_mode": resolution.sandbox_mode,
        "reasoning_effort": resolution.reasoning_effort,
        "subagent_max_depth": resolution.subagent_max_depth,
        "spill_threshold_chars": resolution.spill_threshold_chars,
        "settings": {key: value for key, value in stored.items() if key != "id"},
    }


@router.get(
    "/plugins",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_available_plugins(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List the plugins the deployment can offer."""
    plugin_registry().refresh(settings=settings)
    custom_rows = (
        db.execute(
            select(DeepSpaceCustomPreset).where(
                DeepSpaceCustomPreset.tenant_id == str(auth.tenant_id),
                DeepSpaceCustomPreset.user_id == str(auth.user_id),
            )
        )
        .scalars()
        .all()
    )
    custom_presets = [
        (custom_preset_from_settings(_custom_settings(row)), _custom_settings(row))
        for row in custom_rows
    ]
    preset_service = PresetStateService(db)
    return plugin_runtime_summary(
        settings=settings,
        custom_presets=custom_presets,
        custom_settings=preset_service.user_default_custom_settings(
            tenant_id=auth.tenant_id, user_id=auth.user_id
        ),
        default_preset=preset_service.user_default_preset(
            tenant_id=auth.tenant_id, user_id=auth.user_id
        ),
    )


@router.get(
    "/{conversation_id}/plugins",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_conversation_plugins(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the plugins the conversation runs."""
    plugin_registry().refresh(settings=settings)
    service = PluginStateService(db)
    effective = service.effective_plugins(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    if not bool(getattr(settings, "deepspace_plugins_enabled", True)):
        effective = ()
    return {
        "enabled": list(effective),
        "recorded": list(
            service.recorded_plugins(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            )
            or ()
        ),
        "available": [
            {
                "id": manifest.id,
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "source": manifest.source,
                "tools": [tool.name for tool in manifest.tools],
                "enabled_by_default": manifest.enabled_by_default,
            }
            for manifest in plugin_registry().known()
        ],
    }


@router.put(
    "/{conversation_id}/plugins",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_conversation_plugins(
    conversation_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Record the plugins the conversation runs."""
    if not bool(getattr(settings, "deepspace_plugins_enabled", True)):
        raise HTTPException(status_code=409, detail="The deployment has disabled plugins.")
    plugin_registry().refresh(settings=settings)
    requested = payload.get("enabled")
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="The enabled field must be a list.")
    enabled = tuple(str(item) for item in requested)
    service = PluginStateService(db)
    recorded = service.set_plugins(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        enabled=enabled,
    )
    return {
        "enabled": recorded["enabled"],
        "effective": list(
            service.effective_plugins(
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                conversation_id=conversation_id,
            )
        ),
    }


@router.get(
    "/presets",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_agent_presets(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List the agent presets a conversation may run in."""
    published: list[dict[str, Any]] = []
    preset_service = PresetStateService(db)
    custom_settings = preset_service.user_default_custom_settings(
        tenant_id=auth.tenant_id, user_id=auth.user_id
    )
    for preset in list_presets():
        effective_custom_settings = custom_settings if preset.id == CUSTOM_PRESET_ID else None
        resolution = resolve_preset(
            preset.id, settings=settings, custom_settings=effective_custom_settings
        )
        payload: dict[str, Any] = {
            "id": preset.id,
            "name": preset.name,
            "description": preset.description,
            "ptc_enabled": resolution.ptc_enabled,
            "plan_mode_by_default": resolution.plan_mode_by_default,
            "editable": preset.editable,
            "tools": list(resolution.tool_allowlist or ()),
            "sandbox_mode": resolution.sandbox_mode,
            "reasoning_effort": resolution.reasoning_effort,
            "subagent_max_depth": resolution.subagent_max_depth,
            "spill_threshold_chars": resolution.spill_threshold_chars,
        }
        if preset.id == CUSTOM_PRESET_ID and custom_settings is not None:
            payload["settings"] = dict(custom_settings)
        published.append(payload)
    custom_rows = (
        db.execute(
            select(DeepSpaceCustomPreset).where(
                DeepSpaceCustomPreset.tenant_id == str(auth.tenant_id),
                DeepSpaceCustomPreset.user_id == str(auth.user_id),
            )
        )
        .scalars()
        .all()
    )
    published.extend(_custom_preset_payload(row, settings=settings) for row in custom_rows)
    return {
        "presets": published,
        "default": str(getattr(settings, "deepspace_default_preset", "") or "standard"),
    }


@router.post(
    "/presets/custom",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def create_custom_preset(
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Create one named, user-owned custom agent preset."""
    raw_settings = _custom_settings_payload(payload) or {}
    name = str(payload.get("name") or raw_settings.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A custom preset name is required.")
    description = str(
        payload.get("description") or raw_settings.get("description") or "A user-configured preset."
    ).strip()
    raw_settings = dict(raw_settings)
    raw_settings["name"] = name
    raw_settings["description"] = description
    try:
        custom_preset_from_settings(raw_settings)
    except PresetSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing = db.execute(
        select(DeepSpaceCustomPreset).where(
            DeepSpaceCustomPreset.tenant_id == str(auth.tenant_id),
            DeepSpaceCustomPreset.user_id == str(auth.user_id),
            DeepSpaceCustomPreset.name == name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A custom preset with that name already exists."
        )
    row = DeepSpaceCustomPreset(
        id=str(generate_uuid7_with_fallback()),
        tenant_id=str(auth.tenant_id),
        user_id=str(auth.user_id),
        name=name[:120],
        description=description[:400],
        settings_json={
            key: value
            for key, value in raw_settings.items()
            if key not in {"id", "name", "description"}
        },
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"preset": _custom_preset_payload(row, settings=settings)}


@router.put(
    "/presets/custom/{preset_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def update_custom_preset(
    preset_id: str,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Update one saved custom preset and keep its runtime settings durable."""
    row = _custom_row(db, auth=auth, preset_id=preset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Custom preset not found.")
    raw_settings = _custom_settings_payload(payload)
    if raw_settings is None:
        raw_settings = dict(row.settings_json or {})
    else:
        raw_settings = dict(raw_settings)
    name = str(payload.get("name") or raw_settings.get("name") or row.name).strip()
    description = str(
        payload.get("description") or raw_settings.get("description") or row.description
    ).strip()
    raw_settings["name"] = name
    raw_settings["description"] = description
    raw_settings["id"] = _custom_id(row)
    try:
        custom_preset_from_settings(raw_settings)
    except PresetSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conflict = db.execute(
        select(DeepSpaceCustomPreset).where(
            DeepSpaceCustomPreset.tenant_id == str(auth.tenant_id),
            DeepSpaceCustomPreset.user_id == str(auth.user_id),
            DeepSpaceCustomPreset.name == name,
            DeepSpaceCustomPreset.id != row.id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise HTTPException(
            status_code=409, detail="A custom preset with that name already exists."
        )
    row.name = name[:120]
    row.description = description[:400]
    row.settings_json = {
        key: value
        for key, value in raw_settings.items()
        if key not in {"id", "name", "description"}
    }
    db.commit()
    db.refresh(row)
    return {"preset": _custom_preset_payload(row, settings=settings)}


@router.delete(
    "/presets/custom/{preset_id}",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def delete_custom_preset(
    preset_id: str,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Delete one saved custom preset, falling back to Standard if needed."""
    row = _custom_row(db, auth=auth, preset_id=preset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Custom preset not found.")
    service = PresetStateService(db)
    if service.user_default_preset(tenant_id=auth.tenant_id, user_id=auth.user_id) == _custom_id(
        row
    ):
        service.set_preset(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=user_default_conversation_id(auth.tenant_id, auth.user_id),
            preset_id=DEFAULT_PRESET_ID,
        )
    db.delete(row)
    db.commit()
    return {
        "deleted": _custom_id(row),
        "default": DEFAULT_PRESET_ID,
        "deployment_default": str(
            getattr(settings, "deepspace_default_preset", "") or DEFAULT_PRESET_ID
        ),
    }


@router.get(
    "/{conversation_id}/preset",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_conversation_preset(
    conversation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the agent preset the conversation runs in."""
    service = PresetStateService(db)
    deployment_default = str(getattr(settings, "deepspace_default_preset", "") or "standard")
    recorded = service.recorded_preset(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    recorded_settings = service.recorded_preset_settings(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
    )
    effective, effective_settings = service.effective_preset_entry(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=conversation_id,
        deployment_default=deployment_default,
    )
    return {
        "preset": effective,
        "recorded": recorded,
        # A legacy conversation can record only `preset: custom`.  The
        # effective settings then come from the user's saved Custom mode and
        # must be returned to the UI as well as enforced at runtime.
        "settings": recorded_settings if recorded_settings is not None else effective_settings,
        "deployment_default": deployment_default,
    }


@router.put(
    "/{conversation_id}/preset",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_conversation_preset(
    conversation_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Record the agent preset the conversation runs in.

    The custom preset carries the settings the user edited, so they are
    recorded with the preset and read back by the next turn.
    """
    deployment_default = str(getattr(settings, "deepspace_default_preset", "") or DEFAULT_PRESET_ID)
    preset_id = str(payload.get("preset") or "").strip()
    custom_settings = _custom_settings_payload(payload)
    preset_service = PresetStateService(db)
    if preset_id.lower() == CUSTOM_PRESET_ID and custom_settings is None:
        # Choosing the built-in Custom mode for this conversation must carry
        # the user's saved tool list.  Without it, Custom's empty default
        # would mean every tool is available for this conversation.
        custom_settings = preset_service.user_default_custom_settings(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
        )
    if is_custom_preset_id(preset_id) and preset_id.lower() != "custom":
        row = _custom_row(db, auth=auth, preset_id=preset_id)
        if row is None:
            raise HTTPException(status_code=400, detail="Custom preset not found.")
        custom_settings = _custom_settings(row)
    # Reject hidden presets before persisting.
    try:
        PresetEnablementService(db).assert_preset_available(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            preset_id=preset_id,
        )
    except (PresetNotFoundError, PresetUnavailableError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        recorded = preset_service.set_preset(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=conversation_id,
            preset_id=preset_id,
            custom_settings=custom_settings,
        )
    except PresetNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PresetSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"preset": recorded["preset"], "deployment_default": deployment_default}


@router.get(
    "/presets/default",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def get_user_default_preset(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the default preset the user chose for new conversations."""
    deployment_default = str(getattr(settings, "deepspace_default_preset", "") or DEFAULT_PRESET_ID)
    service = PresetStateService(db)
    recorded = service.user_default_preset(tenant_id=auth.tenant_id, user_id=auth.user_id)
    recorded_settings = service.recorded_preset_settings(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=user_default_conversation_id(auth.tenant_id, auth.user_id),
    )
    custom_settings = service.user_default_custom_settings(
        tenant_id=auth.tenant_id, user_id=auth.user_id
    )
    return {
        "preset": recorded or deployment_default,
        "recorded": recorded,
        "settings": recorded_settings,
        "custom_settings": custom_settings,
        "deployment_default": deployment_default,
    }


@router.put(
    "/presets/default",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_user_default_preset(
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Record the default preset the user chose for new conversations.

    The custom settings travel with the preset, so every new conversation
    that inherits the default also inherits the custom preset settings.
    """
    deployment_default = str(getattr(settings, "deepspace_default_preset", "") or DEFAULT_PRESET_ID)
    preset_id = str(payload.get("preset") or "").strip()
    custom_settings = _custom_settings_payload(payload)
    if is_custom_preset_id(preset_id) and preset_id.lower() != "custom":
        row = _custom_row(db, auth=auth, preset_id=preset_id)
        if row is None:
            raise HTTPException(status_code=400, detail="Custom preset not found.")
        custom_settings = _custom_settings(row)
    try:
        PresetEnablementService(db).assert_preset_available(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            preset_id=preset_id,
            deployment_default=deployment_default,
        )
        service = PresetStateService(db)
        recorded = service.set_preset(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            conversation_id=user_default_conversation_id(auth.tenant_id, auth.user_id),
            preset_id=preset_id,
            custom_settings=custom_settings,
        )
    except (PresetNotFoundError, PresetUnavailableError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PresetSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    recorded_settings = service.recorded_preset_settings(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        conversation_id=user_default_conversation_id(auth.tenant_id, auth.user_id),
    )
    custom_settings = service.user_default_custom_settings(
        tenant_id=auth.tenant_id, user_id=auth.user_id
    )
    return {
        "preset": recorded["preset"],
        "recorded": recorded["preset"],
        "settings": recorded_settings,
        "custom_settings": custom_settings,
        "deployment_default": deployment_default,
    }


@router.get(
    "/presets/enablement",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def list_preset_enablement(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read the enablement state of every agent mode."""
    del settings
    service = PresetEnablementService(db)
    states = service.enabled_map(tenant_id=auth.tenant_id, user_id=auth.user_id)
    return {
        "enablement": [
            {"preset_id": preset_id, "enabled": enabled} for preset_id, enabled in states.items()
        ],
    }


@router.put(
    "/presets/enablement",
    dependencies=[Depends(require_permissions("queries:run"))],
)
def set_preset_enablement(
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Record whether the user's mode selector lists one preset."""
    deployment_default = str(getattr(settings, "deepspace_default_preset", "") or DEFAULT_PRESET_ID)
    preset_id = str(payload.get("preset_id") or "").strip()
    enabled = bool(payload.get("enabled"))
    try:
        states = PresetEnablementService(db).set_enabled(
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            preset_id=preset_id,
            enabled=enabled,
            deployment_default=deployment_default,
        )
    except PresetNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PresetSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"enablement": [{"preset_id": pid, "enabled": on} for pid, on in states.items()]}
