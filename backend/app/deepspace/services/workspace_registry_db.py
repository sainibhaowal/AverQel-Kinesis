"""Database-backed workspace registry service.

Replaces the filesystem manifest (`.workspaces/.registry.json`) with a
proper database store, enabling multi-device sync and web deployments.
Filesystem markers (`.deepspace-workspace.json`) are still written for
portability and desktop offline operation.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.deepspace.models.conversation import Conversation
from app.deepspace.models.workspace_registry import (
    WorkspaceDeletedLog,
    WorkspaceOrder,
    WorkspaceSessionPosition,
)
from app.deepspace.models.workspace_registry import (
    WorkspaceRegistry as WorkspaceRegistryModel,
)
from app.deepspace.services.workspace_registry import (
    QUARANTINE_DIRNAME,
    QUARANTINE_PREFIX,
    REGISTRY_VERSION,
    WORKSPACE_MARKER_FILENAME,
    UnknownWorkspaceRegistryError,
    WorkspaceNameConflictError,
    WorkspaceRegistryError,
    atomic_write_json,
    read_json_object,
)
from app.deepspace.services.workspace_root import (
    DEFAULT_WORKSPACE_ID,
    account_workspace_root,
    is_workspace_id,
    named_workspaces_root,
)

#: The name the default workspace carries in the catalogue.
DEFAULT_WORKSPACE_NAME = "Default"


@dataclass(frozen=True, slots=True)
class WorkspaceProjectSession:
    """One conversation in a project, for the sidebar API."""

    id: str
    title: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class WorkspaceProject:
    """One project with its sessions, for the sidebar API."""

    id: str
    slug: str
    title: str
    path: str
    custom_path: str | None
    is_default: bool
    status: str
    display_order: int
    sessions: list[WorkspaceProjectSession]


class WorkspaceRegistryService:
    """Database-backed workspace registry for one account."""

    def __init__(
        self,
        db: Session,
        settings: Any,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        self.db = db
        self.settings = settings
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.named_root = named_workspaces_root(settings, tenant_id, user_id)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def catalogue(self) -> list[WorkspaceRegistryModel]:
        """Return all named workspaces in user's display order."""
        stmt = (
            select(WorkspaceRegistryModel)
            .where(
                WorkspaceRegistryModel.tenant_id == self.tenant_id,
                WorkspaceRegistryModel.user_id == self.user_id,
                WorkspaceRegistryModel.is_default.is_(False),
            )
            .join(
                WorkspaceOrder,
                WorkspaceOrder.workspace_id == WorkspaceRegistryModel.id,
            )
            .where(
                WorkspaceOrder.tenant_id == self.tenant_id,
                WorkspaceOrder.user_id == self.user_id,
            )
            .order_by(WorkspaceOrder.position)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_workspace(self, workspace_id: uuid.UUID) -> WorkspaceRegistryModel | None:
        """Get one workspace by UUID."""
        stmt = select(WorkspaceRegistryModel).where(
            WorkspaceRegistryModel.id == workspace_id,
            WorkspaceRegistryModel.tenant_id == self.tenant_id,
            WorkspaceRegistryModel.user_id == self.user_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_workspace_by_slug(self, slug: str) -> WorkspaceRegistryModel | None:
        """Get one workspace by slug."""
        stmt = select(WorkspaceRegistryModel).where(
            WorkspaceRegistryModel.slug == slug,
            WorkspaceRegistryModel.tenant_id == self.tenant_id,
            WorkspaceRegistryModel.user_id == self.user_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_default_workspace(self) -> WorkspaceRegistryModel | None:
        """Get the default workspace for this account."""
        stmt = select(WorkspaceRegistryModel).where(
            WorkspaceRegistryModel.tenant_id == self.tenant_id,
            WorkspaceRegistryModel.user_id == self.user_id,
            WorkspaceRegistryModel.is_default.is_(True),
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_workspace_with_sessions(
        self, workspace_id: uuid.UUID
    ) -> tuple[WorkspaceRegistryModel | None, list[WorkspaceProjectSession]]:
        """Get workspace and its ordered sessions."""
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            return None, []

        sessions = self._get_sessions_for_workspace(workspace_id)
        return workspace, sessions

    def _get_sessions_for_workspace(self, workspace_id: uuid.UUID) -> list[WorkspaceProjectSession]:
        """Get ordered sessions for a workspace."""
        stmt = (
            select(WorkspaceSessionPosition, Conversation)
            .join(Conversation, Conversation.id == WorkspaceSessionPosition.conversation_id)
            .where(WorkspaceSessionPosition.workspace_id == workspace_id)
            .order_by(WorkspaceSessionPosition.position)
        )
        rows = self.db.execute(stmt).all()
        return [
            WorkspaceProjectSession(
                id=str(conversation.id),
                title=conversation.title or "",
                updated_at=conversation.updated_at.isoformat() if conversation.updated_at else "",
            )
            for _, conversation in rows
        ]

    def get_all_projects(self) -> list[WorkspaceProject]:
        """Get all projects with their sessions for the sidebar."""
        workspaces = self.catalogue()
        default_ws = self.get_default_workspace()

        projects = []

        # Default workspace first
        if default_ws:
            sessions = self._get_sessions_for_workspace(default_ws.id)
            default_root = account_workspace_root(self.settings, self.tenant_id, self.user_id)
            projects.append(
                WorkspaceProject(
                    id=str(default_ws.id),
                    slug=DEFAULT_WORKSPACE_ID,
                    title=DEFAULT_WORKSPACE_NAME,
                    path=str(default_root),
                    custom_path=None,
                    is_default=True,
                    status="ok" if default_root.is_dir() else "missing-dir",
                    display_order=-1,
                    sessions=sessions,
                )
            )

        # Named workspaces in display order
        for ws in workspaces:
            sessions = self._get_sessions_for_workspace(ws.id)
            named_root_str = str(self.named_root)
            projects.append(
                WorkspaceProject(
                    id=str(ws.id),
                    slug=ws.slug,
                    title=ws.title,
                    path=ws.effective_path(named_root_str),
                    custom_path=ws.custom_path,
                    is_default=False,
                    status=(
                        "ok" if Path(ws.effective_path(named_root_str)).is_dir() else "missing-dir"
                    ),
                    display_order=ws.display_order,
                    sessions=sessions,
                )
            )

        return projects

    def session_order(self, workspace_id: uuid.UUID) -> list[str]:
        """Get ordered conversation IDs for a workspace."""
        stmt = (
            select(WorkspaceSessionPosition.conversation_id)
            .where(WorkspaceSessionPosition.workspace_id == workspace_id)
            .order_by(WorkspaceSessionPosition.position)
        )
        return [str(row) for row in self.db.execute(stmt).scalars().all()]

    def default_root(self) -> Path:
        return account_workspace_root(self.settings, self.tenant_id, self.user_id)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create(self, title: str, custom_path: Path | None = None) -> WorkspaceRegistryModel:
        """Create a new workspace registration."""
        from app.deepspace.services.workspaces import validate_workspace_name, workspace_slug

        display_name = validate_workspace_name(title)
        slug = workspace_slug(display_name)

        # Check for duplicate slug
        existing = self.get_workspace_by_slug(slug)
        if existing:
            raise WorkspaceRegistryError(f"A workspace named '{display_name}' already exists.")

        # Check for duplicate custom path
        if custom_path:
            resolved = custom_path.expanduser().resolve()
            for ws in self.catalogue():
                if ws.custom_path and Path(ws.custom_path).expanduser().resolve() == resolved:
                    return ws

        # Determine display order (append to end)
        max_order_stmt = select(func.coalesce(func.max(WorkspaceOrder.position), -1)).where(
            WorkspaceOrder.tenant_id == self.tenant_id,
            WorkspaceOrder.user_id == self.user_id,
        )
        max_order = self.db.execute(max_order_stmt).scalar() or -1
        new_order = max_order + 1

        # Create filesystem registration directory and marker
        reg_dir = self.named_root / slug
        if reg_dir.exists():
            raise WorkspaceRegistryError(f"The workspace directory '{reg_dir}' already exists.")

        reg_dir.mkdir(parents=True, exist_ok=True)

        # Create database record
        workspace = WorkspaceRegistryModel(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            slug=slug,
            title=display_name,
            custom_path=str(custom_path) if custom_path else None,
            is_default=False,
            display_order=0,  # Not used for named workspaces; order is in WorkspaceOrder
        )
        self.db.add(workspace)
        self.db.flush()

        # Create order entry
        order_entry = WorkspaceOrder(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            workspace_id=workspace.id,
            position=new_order,
        )
        self.db.add(order_entry)

        # Write filesystem marker
        self._write_marker(workspace)

        self.db.commit()
        return workspace

    def rename(self, workspace_id: uuid.UUID, title: str) -> WorkspaceRegistryModel:
        """Rename a workspace (changes title only, not UUID or slug)."""
        from app.deepspace.services.workspaces import validate_workspace_name

        display_name = validate_workspace_name(title)

        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise UnknownWorkspaceRegistryError("No such workspace.")

        if workspace.is_default:
            raise WorkspaceRegistryError("The default workspace cannot be renamed.")

        # Check for title conflict (case-insensitive)
        for ws in self.catalogue():
            if ws.id != workspace_id and ws.title.strip().lower() == display_name.lower():
                raise WorkspaceNameConflictError(
                    f"A workspace named '{display_name}' already exists."
                )

        workspace.title = display_name
        workspace.updated_at = datetime.now(UTC)
        self._write_marker(workspace)
        self.db.commit()
        return workspace

    def set_custom_path(
        self, workspace_id: uuid.UUID, custom_path: Path | None
    ) -> WorkspaceRegistryModel:
        """Attach or detach a custom project directory."""
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise UnknownWorkspaceRegistryError("No such workspace.")

        if workspace.is_default:
            raise WorkspaceRegistryError("The default workspace path cannot be changed.")

        if custom_path:
            resolved = custom_path.expanduser().resolve()
            if not resolved.is_dir():
                raise WorkspaceRegistryError(f"Directory '{resolved}' does not exist.")
            # Check for duplicate
            for ws in self.catalogue():
                if (
                    ws.id != workspace_id
                    and ws.custom_path
                    and Path(ws.custom_path).expanduser().resolve() == resolved
                ):
                    raise WorkspaceRegistryError("Another workspace already uses this directory.")
            workspace.custom_path = str(custom_path)
        else:
            workspace.custom_path = None

        workspace.updated_at = datetime.now(UTC)
        self._write_marker(workspace)
        self.db.commit()
        return workspace

    def move(self, workspace_id: uuid.UUID, before_id: uuid.UUID | None) -> list[uuid.UUID]:
        """Move a workspace in the display order."""
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise UnknownWorkspaceRegistryError("No such workspace.")

        if workspace.is_default:
            raise WorkspaceRegistryError("The default workspace keeps its place.")

        # Get all workspaces in current order
        stmt = (
            select(WorkspaceOrder)
            .where(
                WorkspaceOrder.tenant_id == self.tenant_id,
                WorkspaceOrder.user_id == self.user_id,
            )
            .order_by(WorkspaceOrder.position)
        )
        orders = list(self.db.execute(stmt).scalars().all())

        # Remove the moving workspace
        moving_order = next((o for o in orders if o.workspace_id == workspace_id), None)
        if moving_order is None:
            raise UnknownWorkspaceRegistryError("Workspace not in order list.")

        orders.remove(moving_order)

        # Determine new position
        if before_id is None:
            # Move to end
            new_position = len(orders)
        else:
            before_order = next((o for o in orders if o.workspace_id == before_id), None)
            if before_order is None:
                raise UnknownWorkspaceRegistryError("Target workspace not found.")
            new_position = before_order.position

        # Reassign positions
        for i, order in enumerate(orders):
            if i >= new_position:
                order.position = i + 1
            else:
                order.position = i

        moving_order.position = new_position
        orders.insert(new_position, moving_order)

        self.db.commit()
        return [o.workspace_id for o in orders]

    def delete(self, workspace_id: uuid.UUID) -> bool:
        """Delete a workspace (non-destructive: moves registration to quarantine)."""
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            return False

        if workspace.is_default:
            raise WorkspaceRegistryError("The default workspace cannot be deleted.")

        # Quarantine filesystem registration directory
        quarantine_path = None
        reg_dir = self.named_root / workspace.slug
        if reg_dir.exists() and not reg_dir.is_symlink():
            quarantine = self.named_root / QUARANTINE_DIRNAME
            quarantine.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            target = quarantine / f"{QUARANTINE_PREFIX}{workspace.slug}-{stamp}"
            suffix = 1
            while target.exists():
                target = quarantine / f"{QUARANTINE_PREFIX}{workspace.slug}-{stamp}-{suffix}"
                suffix += 1
            os.replace(reg_dir, target)
            quarantine_path = str(target)

        # Log deletion
        deleted_log = WorkspaceDeletedLog(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            workspace_id=workspace.id,
            slug=workspace.slug,
            title=workspace.title,
            custom_path=workspace.custom_path,
            was_default=workspace.is_default,
            quarantine_path=quarantine_path,
        )
        self.db.add(deleted_log)

        # Remove from order
        self.db.execute(
            delete(WorkspaceOrder).where(
                WorkspaceOrder.workspace_id == workspace_id,
                WorkspaceOrder.tenant_id == self.tenant_id,
                WorkspaceOrder.user_id == self.user_id,
            )
        )

        # Remove session positions
        self.db.execute(
            delete(WorkspaceSessionPosition).where(
                WorkspaceSessionPosition.workspace_id == workspace_id
            )
        )

        # Remove workspace record
        self.db.delete(workspace)
        self.db.commit()
        return True

    def order_sessions(
        self, workspace_id: uuid.UUID, conversation_ids: list[uuid.UUID]
    ) -> list[str]:
        """Replace the explicit session order for a workspace."""
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise UnknownWorkspaceRegistryError("No such workspace.")

        # Clear existing positions
        self.db.execute(
            delete(WorkspaceSessionPosition).where(
                WorkspaceSessionPosition.workspace_id == workspace_id
            )
        )

        # Add new positions
        for i, conv_id in enumerate(conversation_ids):
            pos = WorkspaceSessionPosition(
                workspace_id=workspace_id,
                conversation_id=conv_id,
                position=i,
            )
            self.db.add(pos)

        self.db.commit()
        return [str(cid) for cid in conversation_ids]

    def insert_session(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID, before_id: uuid.UUID | None
    ) -> list[str]:
        """Move one conversation before another within a workspace."""
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise UnknownWorkspaceRegistryError("No such workspace.")

        # Get current positions
        stmt = (
            select(WorkspaceSessionPosition)
            .where(WorkspaceSessionPosition.workspace_id == workspace_id)
            .order_by(WorkspaceSessionPosition.position)
        )
        positions = list(self.db.execute(stmt).scalars().all())

        # Remove if already exists
        existing = next((p for p in positions if p.conversation_id == conversation_id), None)
        if existing:
            self.db.delete(existing)
            positions.remove(existing)

        # Find insertion point
        if before_id is None:
            insert_at = len(positions)
        else:
            before_pos = next((p for p in positions if p.conversation_id == before_id), None)
            insert_at = before_pos.position if before_pos else len(positions)

        # Shift positions >= insert_at
        for pos in positions:
            if pos.position >= insert_at:
                pos.position += 1

        # Add new position
        new_pos = WorkspaceSessionPosition(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            position=insert_at,
        )
        self.db.add(new_pos)
        positions.insert(insert_at, new_pos)

        self.db.commit()
        return [str(p.conversation_id) for p in positions]

    def prune_sessions(self, valid_conversation_ids: set[uuid.UUID]) -> None:
        """Remove positions for conversations that no longer exist."""
        stmt = select(WorkspaceSessionPosition.conversation_id).where(
            WorkspaceSessionPosition.conversation_id.not_in(valid_conversation_ids)
        )
        stale = set(self.db.execute(stmt).scalars().all())
        if stale:
            self.db.execute(
                delete(WorkspaceSessionPosition).where(
                    WorkspaceSessionPosition.conversation_id.in_(stale)
                )
            )
            self.db.commit()

    def reconcile_from_filesystem(self) -> int:
        """Import any registration directories not yet in the database."""
        imported = 0
        try:
            entries = sorted(self.named_root.iterdir(), key=lambda p: p.name)
        except OSError:
            return 0

        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if not entry.is_dir() or not is_workspace_id(entry.name):
                continue

            # Check if already in DB
            existing = self.get_workspace_by_slug(entry.name)
            if existing:
                continue

            # Read marker
            marker = read_json_object(entry / WORKSPACE_MARKER_FILENAME) or {}
            title = str(marker.get("title") or marker.get("name") or entry.name)
            custom_path = marker.get("path")
            created_at = marker.get("created_at")
            updated_at = marker.get("updated_at")

            # Create DB record
            max_order_stmt = select(func.coalesce(func.max(WorkspaceOrder.position), -1)).where(
                WorkspaceOrder.tenant_id == self.tenant_id,
                WorkspaceOrder.user_id == self.user_id,
            )
            max_order = self.db.execute(max_order_stmt).scalar() or -1

            workspace = WorkspaceRegistryModel(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                slug=entry.name,
                title=title,
                custom_path=str(custom_path) if custom_path else None,
                is_default=False,
                display_order=0,
                created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(UTC),
                updated_at=datetime.fromisoformat(updated_at) if updated_at else datetime.now(UTC),
            )
            self.db.add(workspace)
            self.db.flush()

            order_entry = WorkspaceOrder(
                tenant_id=self.tenant_id,
                user_id=self.user_id,
                workspace_id=workspace.id,
                position=max_order + 1,
            )
            self.db.add(order_entry)
            imported += 1

        if imported:
            self.db.commit()
        return imported

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_marker(self, workspace: WorkspaceRegistryModel) -> None:
        """Write the portable filesystem marker."""
        reg_dir = self.named_root / workspace.slug
        reg_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            reg_dir / WORKSPACE_MARKER_FILENAME,
            {
                "version": REGISTRY_VERSION,
                "id": str(workspace.id),
                "slug": workspace.slug,
                "title": workspace.title,
                "path": workspace.custom_path,
                "created_at": workspace.created_at.isoformat(),
                "updated_at": workspace.updated_at.isoformat(),
            },
        )

    def ensure_default_workspace(self) -> WorkspaceRegistryModel:
        """Ensure the default workspace exists in the database."""
        default = self.get_default_workspace()
        if default:
            return default

        default = WorkspaceRegistryModel(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            slug=DEFAULT_WORKSPACE_ID,
            title=DEFAULT_WORKSPACE_NAME,
            custom_path=None,
            is_default=True,
            display_order=0,
        )
        self.db.add(default)
        self.db.commit()
        return default


def get_registry_service(
    db: Session, settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceRegistryService:
    """Factory for the registry service."""
    return WorkspaceRegistryService(db, settings, tenant_id, user_id)


__all__ = [
    "WorkspaceProjectSession",
    "WorkspaceProject",
    "WorkspaceRegistryService",
    "get_registry_service",
]
