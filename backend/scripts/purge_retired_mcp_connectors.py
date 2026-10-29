"""Remove MCP connector servers that AverQel no longer publishes.

Remote apps are brokered by Composio now. Servers created from the retired
curated catalog, from the removed placeholder endpoints, or from a hand-entered
server that no longer resolves cannot authenticate, so they only add failing
rows to the connections list.

The rule keeps two kinds of connector:

* Local ``stdio`` tools that are currently connected. They run on this machine
  and need no provider account.
* Composio-brokered apps, identified by their ``composio:`` provider slug.

Every other MCP server owned by the given user is deleted. Deleting a server
cascades to its OAuth tokens, lifecycle events, and connection policy.

Usage::

    python scripts/purge_retired_mcp_connectors.py --user-email you@example.com
    python scripts/purge_retired_mcp_connectors.py --user-email you@example.com --apply

The script is a dry run unless ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.auth.models.tenant import Tenant  # noqa: E402,F401
from app.auth.models.user import User  # noqa: E402
from app.integrations.models.mcp_server import MCPRegistryEntry, MCPServer  # noqa: E402
from app.integrations.services.composio_service import is_composio_server  # noqa: E402
from app.platform.database.session import get_session_factory  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetiredServer:
    """One connector server selected for deletion, with its reason."""

    server_id: uuid.UUID
    name: str
    status: str
    transport: str
    reason: str


def _reason_to_retire(server: MCPServer, entry: MCPRegistryEntry | None) -> str | None:
    """Return why a server should be removed, or ``None`` to keep it."""
    if is_composio_server(server):
        return None
    if server.transport == "stdio":
        if server.status == "connected":
            return None
        return "local tool is not connected"
    if entry is None:
        return "not published by any catalog entry"
    return f"retired catalog entry {entry.server_name!r}"


def collect_retired_servers(db, *, user_email: str | None) -> list[RetiredServer]:
    """Return the user's MCP servers that AverQel no longer publishes."""
    statement = select(MCPServer)
    if user_email is not None:
        user = db.execute(select(User).where(User.email == user_email)).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"No user found with email {user_email!r}")
        statement = statement.where(MCPServer.user_id == user.id)
    servers = db.execute(statement).scalars().all()
    entries = {entry.id: entry for entry in db.execute(select(MCPRegistryEntry)).scalars().all()}
    retired: list[RetiredServer] = []
    for server in servers:
        entry = entries.get(server.registry_entry_id)
        reason = _reason_to_retire(server, entry)
        if reason is None:
            continue
        retired.append(
            RetiredServer(
                server_id=server.id,
                name=server.name,
                status=server.status,
                transport=server.transport,
                reason=reason,
            )
        )
    return retired


def purge_retired_servers(db, *, user_email: str | None, apply: bool) -> int:
    """Delete every retired connector server and return how many were removed."""
    retired = collect_retired_servers(db, user_email=user_email)
    for item in retired:
        logger.info("retire %s [%s/%s] %s", item.name, item.status, item.transport, item.reason)
    if not apply:
        logger.info("Dry run: %d server(s) would be deleted", len(retired))
        return 0
    for item in retired:
        server = db.get(MCPServer, item.server_id)
        if server is not None:
            db.delete(server)
    db.commit()
    logger.info("Deleted %d retired MCP server(s)", len(retired))
    return len(retired)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-email", default=None, help="limit to one account's servers")
    parser.add_argument("--apply", action="store_true", help="delete instead of reporting")
    args = parser.parse_args()
    with get_session_factory()() as db:
        purge_retired_servers(db, user_email=args.user_email, apply=args.apply)


if __name__ == "__main__":
    main()
