"""Ownership adapters for authenticated Typert resource invocations."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _owns_uuid_row(
    model: Any,
    identity: str,
    auth: AuthContext,
    db: Session,
    *,
    user_column: Any | None = None,
) -> bool:
    resource_id = _uuid(identity)
    if resource_id is None:
        return False
    conditions = [model.id == resource_id, model.tenant_id == auth.tenant_id]
    if user_column is not None:
        conditions.append(user_column == auth.user_id)
    return db.execute(select(model.id).where(*conditions)).scalar_one_or_none() is not None


def _owns_conversation(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.models.conversation import Conversation

    return _owns_uuid_row(Conversation, identity, auth, db, user_column=Conversation.user_id)


def _owns_workspace(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.models.workspace_registry import WorkspaceRegistry

    return _owns_uuid_row(
        WorkspaceRegistry, identity, auth, db, user_column=WorkspaceRegistry.user_id
    )


def _owns_library_file(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.models.workspace_file import DeepSpaceWorkspaceFile

    return _owns_uuid_row(
        DeepSpaceWorkspaceFile,
        identity,
        auth,
        db,
        user_column=DeepSpaceWorkspaceFile.user_id,
    )


def _owns_document(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.documents.models.document import Document

    return _owns_uuid_row(
        Document,
        identity,
        auth,
        db,
        user_column=Document.uploaded_by_user_id,
    )


def _owns_ssh_host(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.models.ssh_host import DeepSpaceSshHost

    return _owns_uuid_row(
        DeepSpaceSshHost, identity, auth, db, user_column=DeepSpaceSshHost.user_id
    )


def _owns_media_artifact(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.models.media_artifact import DeepSpaceMediaArtifact

    return _owns_uuid_row(
        DeepSpaceMediaArtifact,
        identity,
        auth,
        db,
        user_column=DeepSpaceMediaArtifact.user_id,
    )


def _owns_workflow_job(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.models.workflow_job import DeepSpaceWorkflowJob

    return _owns_uuid_row(
        DeepSpaceWorkflowJob,
        identity,
        auth,
        db,
        user_column=DeepSpaceWorkflowJob.user_id,
    )


def _owns_schedule(db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.models.schedule import DeepSpaceSchedule

    return _owns_uuid_row(
        DeepSpaceSchedule, identity, auth, db, user_column=DeepSpaceSchedule.user_id
    )


def _owns_plugin(db: Session, auth: AuthContext, identity: str) -> bool:
    """Authorize deployment plugins or plugins in this account's workspaces."""
    plugin_id = str(identity).strip()
    if not plugin_id or len(plugin_id) > 256:
        return False

    from app.deepspace.services.plugins import (
        discover_workspace_plugins,
        plugin_registry,
    )

    shared_manifest = plugin_registry().get(plugin_id)
    if shared_manifest is not None and shared_manifest.source != "workspace":
        # These plugins are installed by the deployment and offered to every
        # account; they contain no per-user plugin state.
        return True

    from app.core.config import get_settings
    from app.deepspace.services.workspace_registry_db import WorkspaceRegistryService

    workspaces = WorkspaceRegistryService(
        db,
        get_settings(),
        auth.tenant_id,
        auth.user_id,
    ).get_all_projects()
    for workspace in workspaces:
        root = Path(workspace.path)
        if not root.is_dir():
            continue
        if any(manifest.id == plugin_id for manifest in discover_workspace_plugins(root)):
            return True
    return False


def _owns_terminal(_db: Session, auth: AuthContext, identity: str) -> bool:
    from app.deepspace.services.terminals import terminal_manager

    return bool(
        terminal_manager().owns_identity(
            identity,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
        )
    )


def built_in_resource_authorizers() -> dict[str, Callable[[Session, AuthContext, str], bool]]:
    """Return only ownership checks backed by an authoritative owner store.

    Deployment plugins are shared by the app. Workspace plugins are checked
    against roots returned by the authenticated account's workspace registry.
    Unknown plugin ids and workspace lookup failures remain denied.
    """
    return {
        "workspace": _owns_workspace,
        "library": _owns_library_file,
        "conversation": _owns_conversation,
        "document": _owns_document,
        "terminal": _owns_terminal,
        "ssh-host": _owns_ssh_host,
        "plugin": _owns_plugin,
        "artifact": _owns_media_artifact,
        "job": _owns_workflow_job,
        "schedule": _owns_schedule,
    }


__all__ = ["built_in_resource_authorizers"]
