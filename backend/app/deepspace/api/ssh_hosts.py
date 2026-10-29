"""The reviewed-host routes for DeepSpace.

A host is added by someone who has checked the machine's identity, and
the entry carries the host key the connection must present.  The private
key is named, never stored: the deployment resolves the reference to a
file on the server.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_auth_context
from app.auth.rbac import require_permissions
from app.core.config import Settings, get_settings
from app.deepspace.models.ssh_host import DeepSpaceSshHost
from app.deepspace.services.ssh import SshHostStore, host_summary
from app.platform.database.session import get_db

router = APIRouter(prefix="/deepspace/ssh-hosts", tags=["deepspace-ssh"])

#: The largest known-hosts entry a host may carry.
MAX_KNOWN_HOSTS_ENTRY = 4_000


@router.get("", dependencies=[Depends(require_permissions("queries:run"))])
def list_ssh_hosts(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List the reviewed remote hosts."""
    hosts = SshHostStore(db).list(tenant_id=auth.tenant_id, user_id=auth.user_id)
    return {"hosts": [host_summary(host) for host in hosts]}


@router.post("", dependencies=[Depends(require_permissions("queries:run"))], status_code=201)
def create_ssh_host(
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Add one reviewed remote host."""
    name = str(payload.get("name") or "").strip()
    hostname = str(payload.get("hostname") or "").strip()
    username = str(payload.get("username") or "").strip()
    known_hosts_entry = str(payload.get("known_hosts_entry") or "").strip()
    if not name or not hostname or not username:
        raise HTTPException(
            status_code=400, detail="A host needs a name, a hostname, and a username."
        )
    if not known_hosts_entry:
        raise HTTPException(
            status_code=400,
            detail="A host needs the host key it must present, so a changed key is refused.",
        )
    if len(known_hosts_entry) > MAX_KNOWN_HOSTS_ENTRY:
        raise HTTPException(status_code=400, detail="That host key entry is too long.")
    port = int(payload.get("port") or 22)
    if not 1 <= port <= 65_535:
        raise HTTPException(status_code=400, detail="That port is not valid.")
    existing = SshHostStore(db).list(tenant_id=auth.tenant_id, user_id=auth.user_id)
    if any(host.name == name for host in existing):
        raise HTTPException(status_code=409, detail="A host with that name already exists.")
    row = DeepSpaceSshHost(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        name=name,
        hostname=hostname,
        port=port,
        username=username,
        known_hosts_entry=known_hosts_entry,
        key_secret_ref=str(payload.get("key_secret_ref") or "").strip() or None,
        remote_workspace=str(payload.get("remote_workspace") or "").strip() or None,
        enabled=bool(payload.get("enabled", True)),
        timeout_seconds=int(payload.get("timeout_seconds") or 60),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    from app.deepspace.services.ssh import host_from_row

    return {"host": host_summary(host_from_row(row))}


@router.patch("/{host_id}", dependencies=[Depends(require_permissions("queries:run"))])
def update_ssh_host(
    host_id: uuid.UUID,
    payload: dict[str, Any],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enable, disable, or re-point one reviewed remote host."""
    row = (
        db.query(DeepSpaceSshHost)
        .filter(
            DeepSpaceSshHost.id == host_id,
            DeepSpaceSshHost.tenant_id == auth.tenant_id,
            DeepSpaceSshHost.user_id == auth.user_id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such SSH host.")
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    if "remote_workspace" in payload:
        row.remote_workspace = str(payload["remote_workspace"] or "").strip() or None
    if "timeout_seconds" in payload:
        row.timeout_seconds = max(5, min(3_600, int(payload["timeout_seconds"] or 60)))
    if "key_secret_ref" in payload:
        row.key_secret_ref = str(payload["key_secret_ref"] or "").strip() or None
    db.commit()
    db.refresh(row)
    from app.deepspace.services.ssh import host_from_row

    return {"host": host_summary(host_from_row(row))}


@router.delete("/{host_id}", dependencies=[Depends(require_permissions("queries:run"))])
def delete_ssh_host(
    host_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Remove one reviewed remote host."""
    row = (
        db.query(DeepSpaceSshHost)
        .filter(
            DeepSpaceSshHost.id == host_id,
            DeepSpaceSshHost.tenant_id == auth.tenant_id,
            DeepSpaceSshHost.user_id == auth.user_id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such SSH host.")
    db.delete(row)
    db.commit()
    return {"deleted": True}


__all__ = ["router"]
