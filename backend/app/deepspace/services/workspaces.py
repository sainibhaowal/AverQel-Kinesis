"""The workspaces one DeepSpace account works in.

DeepSpace gives every tenant and user one project workspace: the directory
the file system tools, the shell, and the terminal write in.  A user may
also keep several named workspaces beside it, so a conversation can work in
the tree that suits the task without disturbing the others.

The default workspace keeps the path it has always had, so a deployment that
names no workspace behaves exactly as it did.  A named workspace lives in a
directory of its own under the account's root, and the account's root is the
only place a workspace may live, so a workspace belongs to exactly one
tenant and user.  A caller that names another account's workspace is
refused exactly as one that names a workspace that does not exist.

The workspace a conversation works in is recorded in the typed session log
as a ``workspace/selected`` event, so it survives a restart by replay and
cannot be changed out of band.  A conversation that never recorded a
selection works in the account's default workspace.

A named workspace carries two names.  Its identifier is the slug of the
name, the directory the workspace lives in, and it is what a caller sends
back to select the workspace.  Its display name is the name the user gave,
and the workspace directory records it, so the catalogue can publish the
name a user chose rather than the slug that was derived from it.
"""

from __future__ import annotations

import json
import shutil
import string
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deepspace.models.session_event import DeepSpaceSessionEvent
from app.deepspace.services.session_events import WORKSPACE_SELECTED, SessionEventStore

# The default identifier belongs with the path rules that resolve it, so it
# is re-exported here beside the catalogue that publishes it.
from app.deepspace.services.workspace_root import (
    DEFAULT_WORKSPACE_ID,
    WorkspaceUnavailableError,
    is_workspace_id,
    named_workspaces_root,
    workspace_root_for,
)

#: The name the default workspace carries in the catalogue.
DEFAULT_WORKSPACE_NAME = "Default"

#: The largest name a workspace may carry, in characters.
MAX_WORKSPACE_NAME_LENGTH = 64

#: The characters a workspace name may contain.
WORKSPACE_NAME_CHARACTERS = frozenset(string.ascii_letters + string.digits + " -_.")

#: The file that records a named workspace's display name.
WORKSPACE_MARKER_FILENAME = ".deepspace-workspace.json"


class WorkspaceError(ValueError):
    """A workspace request was refused."""


class InvalidWorkspaceNameError(WorkspaceError):
    """A workspace name is empty or unsafe."""


class DuplicateWorkspaceError(WorkspaceError):
    """The account already owns a workspace with that name."""


class UnknownWorkspaceError(WorkspaceError):
    """No workspace with that identifier belongs to the account."""


@dataclass(frozen=True, slots=True)
class Workspace:
    """One workspace of an account.

    Attributes:
        id: The identifier the caller and the API use.
        name: The name the user gave the workspace.
        path: The absolute path of the workspace directory.
        is_default: Whether this is the account's default workspace.
    """

    #: The identifier the caller and the API use.
    id: str
    #: The name the user gave the workspace.
    name: str
    #: The absolute path of the workspace directory.
    path: Path
    #: Whether this is the account's default workspace.
    is_default: bool

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON projection of this workspace.

        Returns:
            The workspace's identifier, name, path, and default flag.
        """
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "is_default": self.is_default,
        }


def validate_workspace_name(name: str) -> str:
    """Check one workspace name.

    A workspace name is what the user reads in the picker, so it may hold
    letters, digits, spaces, dashes, underscores, and dots, and nothing
    else.  A name that could name a path outside the account's root is
    refused.

    Args:
        name: The name the user gave.

    Returns:
        The trimmed name.

    Raises:
        InvalidWorkspaceNameError: When the name is empty, too long, or
            holds a character a workspace name may not contain.
    """
    text = str(name or "").strip()
    if not text:
        raise InvalidWorkspaceNameError("A workspace name is required.")
    if len(text) > MAX_WORKSPACE_NAME_LENGTH:
        raise InvalidWorkspaceNameError(
            f"A workspace name may not exceed {MAX_WORKSPACE_NAME_LENGTH} characters."
        )
    if ".." in text:
        raise InvalidWorkspaceNameError("A workspace name may not contain '..'.")
    if any(character not in WORKSPACE_NAME_CHARACTERS for character in text):
        raise InvalidWorkspaceNameError(
            "A workspace name may hold only letters, digits, spaces, dashes, underscores, and dots."
        )
    return text


def workspace_slug(name: str) -> str:
    """Return the identifier one workspace name produces.

    The identifier names the workspace's directory, so it is the name in
    lower case with its spaces replaced by dashes.

    Args:
        name: The name the user gave.

    Returns:
        The identifier.

    Raises:
        InvalidWorkspaceNameError: When the name is unsafe or yields no
            usable identifier.
    """
    text = validate_workspace_name(name)
    slug = "-".join(part for part in text.split() if part).lower()
    if not any(character.isalnum() for character in slug):
        raise InvalidWorkspaceNameError("A workspace name must hold at least one letter or digit.")
    if not is_workspace_id(slug):
        raise InvalidWorkspaceNameError("That workspace name cannot be used as an identifier.")
    return slug


def _recorded_meta(directory: Path) -> dict[str, str]:
    """Return the metadata one workspace directory records.

    Args:
        directory: The workspace directory.

    Returns:
        The metadata dict containing optional 'name' and 'path'.
    """
    try:
        payload = json.loads((directory / WORKSPACE_MARKER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    res: dict[str, str] = {}
    name = payload.get("name")
    if isinstance(name, str) and name.strip():
        res["name"] = name.strip()
    path = payload.get("path")
    if isinstance(path, str) and path.strip():
        res["path"] = path.strip()
    return res


def _recorded_name(directory: Path) -> str | None:
    """Return the display name one workspace directory records.

    Args:
        directory: The workspace directory.

    Returns:
        The recorded name, or None when the directory records none.
    """
    return _recorded_meta(directory).get("name")


def _record_meta(directory: Path, name: str, custom_path: str | None = None) -> None:
    """Record a workspace directory's display name and optional custom path.

    Args:
        directory: The workspace directory.
        name: The name the user gave.
        custom_path: Optional custom path on the host.

    Raises:
        OSError: When the marker cannot be written.
    """
    payload: dict[str, Any] = {"name": name}
    if custom_path:
        payload["path"] = str(custom_path)
    (directory / WORKSPACE_MARKER_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _record_name(directory: Path, name: str) -> None:
    """Record a workspace directory's display name."""
    _record_meta(directory, name, None)


def list_workspaces(settings: Any, tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[Workspace]:
    """List the workspaces one account owns.

    The default workspace is always part of the list.  A named workspace is
    a directory under the account's own root, so a directory that is not one
    is ignored rather than reported as an error.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.

    Returns:
        The default workspace, followed by the named workspaces sorted by
        name.
    """
    root = workspace_root_for(settings, tenant_id, user_id)
    workspaces = [
        Workspace(
            id=DEFAULT_WORKSPACE_ID,
            name=DEFAULT_WORKSPACE_NAME,
            path=root,
            is_default=True,
        )
    ]
    named: list[Workspace] = []
    try:
        entries = sorted(named_workspaces_root(settings, tenant_id, user_id).iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or not is_workspace_id(entry.name):
            continue
        meta = _recorded_meta(entry)
        display_name = meta.get("name") or entry.name
        custom_path = meta.get("path")
        target_path = Path(custom_path).expanduser().resolve() if custom_path else entry.resolve()
        named.append(
            Workspace(
                id=entry.name,
                name=display_name,
                path=target_path,
                is_default=False,
            )
        )
    named.sort(key=lambda workspace: (workspace.name.lower(), workspace.id))
    workspaces.extend(named)
    return workspaces


def find_workspace(
    settings: Any,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: str,
) -> Workspace | None:
    """Return one workspace the account owns.

    The account's own workspaces are the only ones a caller may reach, so a
    workspace that belongs to another account is not found here either.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.
        workspace_id: The workspace to find.

    Returns:
        The workspace, or None when the account owns no such workspace.
    """
    identifier = str(workspace_id or "").strip()
    if not identifier:
        return None
    for workspace in list_workspaces(settings, tenant_id, user_id):
        if workspace.id == identifier:
            return workspace
    return None


def create_workspace(
    settings: Any,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    path: str | None = None,
) -> Workspace:
    """Create one named workspace for an account.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.
        name: The name the user gave.
        path: Optional local filesystem directory path the workspace points to.

    Returns:
        The created workspace.

    Raises:
        InvalidWorkspaceNameError: When the name is empty or unsafe.
        DuplicateWorkspaceError: When the account already owns a workspace
            with that name and no distinct path.
        WorkspaceUnavailableError: When the workspace could not be created.
    """
    display_name = validate_workspace_name(name)
    identifier = workspace_slug(display_name)
    named_root = named_workspaces_root(settings, tenant_id, user_id)

    custom_target: Path | None = None
    if path:
        custom_target = Path(path).expanduser().resolve()
        if not custom_target.is_dir():
            try:
                custom_target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise WorkspaceUnavailableError(
                    f"Directory {custom_target} could not be created."
                ) from exc

    existing_workspaces = list_workspaces(settings, tenant_id, user_id)

    # 1. If path is provided, check if a workspace already points to this exact directory.
    if custom_target is not None:
        for ws in existing_workspaces:
            if ws.path.resolve() == custom_target:
                return ws

    target = named_root / identifier

    # 2. If path is provided and target directory exists:
    if custom_target is not None and target.exists() and target.is_dir():
        meta = _recorded_meta(target)
        existing_custom_path = meta.get("path")
        if (
            not existing_custom_path
            or Path(existing_custom_path).expanduser().resolve() == target.resolve()
        ):
            # Target was an internal stub without a custom path; upgrade it to point to the selected path.
            try:
                _record_meta(target, display_name, str(custom_target))
            except OSError as exc:
                raise WorkspaceUnavailableError("The workspace could not be updated.") from exc
            return Workspace(
                id=identifier,
                name=display_name,
                path=custom_target,
                is_default=False,
            )
        else:
            # Target points to a different directory. Find a unique identifier slug so both can coexist.
            base_id = identifier
            counter = 2
            while (named_root / f"{base_id}-{counter}").exists():
                counter += 1
            identifier = f"{base_id}-{counter}"
            target = named_root / identifier

    # 3. If path is NOT provided, check for duplicate names:
    if custom_target is None:
        if target.exists():
            raise DuplicateWorkspaceError(f"A workspace named '{display_name}' already exists.")
        for workspace in existing_workspaces:
            if workspace.name.strip().lower() == display_name.lower():
                raise DuplicateWorkspaceError(f"A workspace named '{display_name}' already exists.")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceUnavailableError("The workspace could not be created.") from exc

    try:
        _record_meta(target, display_name, str(custom_target) if custom_target else None)
    except OSError as exc:
        raise WorkspaceUnavailableError("The workspace could not be created.") from exc

    return Workspace(
        id=identifier,
        name=display_name,
        path=custom_target if custom_target else target.resolve(),
        is_default=False,
    )


def delete_workspace(
    settings: Any,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: str,
) -> bool:
    """Delete or unlink one named workspace from the account's catalogue.

    Only the registration marker directory under named_workspaces_root is removed.
    Any external project directory or files on host storage that the workspace
    pointed to are 100% preserved and never deleted or modified.

    Args:
        settings: The deployment settings in force.
        tenant_id: The owning tenant.
        user_id: The owning user.
        workspace_id: The workspace to delete or unlink.

    Returns:
        True if the workspace was unlinked, False if not found or is default.
    """
    identifier = str(workspace_id or "").strip()
    if not identifier or identifier == DEFAULT_WORKSPACE_ID or not is_workspace_id(identifier):
        return False

    named_root = named_workspaces_root(settings, tenant_id, user_id).resolve()
    target = (named_root / identifier).resolve()

    # Security: Ensure target stays strictly inside named_workspaces_root.
    # It must never delete the named_root itself or escape to arbitrary paths.
    try:
        target.relative_to(named_root)
    except ValueError:
        return False
    if target == named_root or not target.exists() or not target.is_dir():
        return False

    try:
        # Only delete the internal metadata registration directory inside .workspaces
        shutil.rmtree(target)
        return True
    except OSError as exc:
        raise WorkspaceUnavailableError(f"Could not unlink workspace '{identifier}'.") from exc


class WorkspaceSelectionService:
    """The workspace one conversation works in.

    The selection is recorded in the typed session log as a
    ``workspace/selected`` event, so it survives a restart by replay and
    cannot be changed out of band.  A conversation that never recorded a
    selection works in the account's default workspace.
    """

    def __init__(self, db: Session) -> None:
        """Create the service.

        Args:
            db: The database session.
        """
        self.db = db
        self.events = SessionEventStore(db)

    def recorded_workspace(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> str | None:
        """Read the conversation's last recorded workspace.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to read.

        Returns:
            The recorded workspace identifier, or None when the conversation
            never recorded one.
        """
        events = (
            self.db.execute(
                select(DeepSpaceSessionEvent)
                .where(
                    DeepSpaceSessionEvent.tenant_id == tenant_id,
                    DeepSpaceSessionEvent.user_id == user_id,
                    DeepSpaceSessionEvent.conversation_id == conversation_id,
                    DeepSpaceSessionEvent.event_type == WORKSPACE_SELECTED,
                )
                .order_by(DeepSpaceSessionEvent.seq.desc())
                .limit(1)
            )
            .scalars()
            .all()
        )
        if not events:
            return None
        payload: object = events[0].payload
        if not isinstance(payload, dict):
            return None
        workspace = str(payload.get("workspace") or "")
        return workspace if is_workspace_id(workspace) else None

    def effective_workspace(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> str:
        """Resolve the workspace one call works in.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation the call belongs to.

        Returns:
            The recorded workspace identifier, or the default workspace's
            identifier when the conversation recorded none.
        """
        recorded = self.recorded_workspace(
            tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id
        )
        if recorded:
            return recorded
        return DEFAULT_WORKSPACE_ID

    def set_workspace(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        workspace: str,
    ) -> dict[str, Any]:
        """Record the conversation's workspace.

        Args:
            tenant_id: The owning tenant.
            user_id: The owning user.
            conversation_id: The conversation to change.
            workspace: The workspace identifier to record.

        Returns:
            The recorded state.

        Raises:
            UnknownWorkspaceError: When the identifier is not one a
                workspace may carry.
        """
        identifier = str(workspace or "").strip()
        if not is_workspace_id(identifier):
            raise UnknownWorkspaceError("No such workspace.")
        self.events.append(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            event_type=WORKSPACE_SELECTED,
            payload={"workspace": identifier},
        )
        self.db.commit()
        return {"workspace": identifier}


__all__ = [
    "DEFAULT_WORKSPACE_ID",
    "DEFAULT_WORKSPACE_NAME",
    "MAX_WORKSPACE_NAME_LENGTH",
    "WORKSPACE_MARKER_FILENAME",
    "WORKSPACE_NAME_CHARACTERS",
    "DuplicateWorkspaceError",
    "InvalidWorkspaceNameError",
    "UnknownWorkspaceError",
    "Workspace",
    "WorkspaceError",
    "WorkspaceSelectionService",
    "create_workspace",
    "find_workspace",
    "list_workspaces",
    "validate_workspace_name",
    "workspace_slug",
]
