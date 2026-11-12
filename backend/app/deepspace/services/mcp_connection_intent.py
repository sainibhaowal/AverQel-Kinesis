"""Find approved local MCP choices for explicit requests that need a connector."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext
from app.deepspace.services.tool_requirements import (
    excludes_mcp_for_prompt,
    is_mcp_catalog_lookup,
    requires_tool_execution,
)
from app.integrations.models.mcp_server import MCPRegistryEntry, MCPServer
from app.integrations.services.mcp_stdio_launch import stdio_launch_details

_EXPLICIT_MCP_ACTION = re.compile(
    r"\b(?:use|using|via|through|with|call|run|execute|access|connect)\b.{0,48}"
    r"\b(?:mcp|connected\s+(?:tools?|services?))\b|"
    r"\b(?:mcp|connected\s+(?:tools?|services?))\b.{0,48}"
    r"\b(?:use|call|run|execute|access|connect|check|find|read|search|open|inspect)\b",
    re.IGNORECASE,
)
_CONDITIONAL_MCP_DISCLOSURE = re.compile(
    r"\bif\s+(?:you\s+)?need\s+to\s+(?:connect|use)\b[^.!?]*(?:[.!?]|$)",
    re.IGNORECASE,
)
_MCP_OPT_OUT = re.compile(
    r"\b(?:do not|don't|dont|never|avoid)\s+(?:use|call|search|query|access)\s+"
    r"(?:the\s+)?(?:mcp|connected\s+(?:tools?|services?))\b|"
    r"\bwithout\s+(?:using\s+)?(?:mcp|connected\s+(?:tools?|services?))\b",
    re.IGNORECASE,
)
_PROVIDER_TERMS: dict[str, frozenset[str]] = {
    "local-filesystem": frozenset(
        {
            "filesystem",
            "file",
            "files",
            "folder",
            "folders",
            "directory",
            "directories",
            "workspace",
        }
    ),
    "local-git": frozenset({"git", "repository", "repo", "commit", "branch"}),
    "local-sqlite": frozenset({"sqlite", "database", "sql"}),
    "local-puppeteer": frozenset({"puppeteer", "browser", "webpage", "website"}),
    "local-memory": frozenset({"memory", "remember", "recall"}),
    "local-fetch": frozenset({"fetch", "url", "website", "webpage"}),
}
_KNOWN_CONNECTOR_TARGETS = frozenset(
    {
        "browser",
        "calendar",
        "cpu",
        "drive",
        "email",
        "gmail",
        "gpu",
        "inbox",
        "nvidia",
        "notion",
        "outlook",
        "slack",
        "spotify",
        "system",
    }
)
_IMPLICIT_CONNECTOR_TARGETS = frozenset(
    {"calendar", "drive", "email", "gmail", "inbox", "notion", "outlook", "slack", "spotify"}
)


def is_explicit_mcp_action_request(prompt: str | None) -> bool:
    """Whether the user explicitly asks to perform an action through MCP."""
    text = " ".join(str(prompt or "").split())
    # A conditional safety instruction is permission to ask before connecting,
    # not a request to route the underlying task through MCP. Remove that
    # disclosure sentence before matching action language such as "what will
    # run" or "choose Connect or Cancel".
    action_text = _CONDITIONAL_MCP_DISCLOSURE.sub(" ", text)
    return bool(
        text
        and not _MCP_OPT_OUT.search(text)
        and requires_tool_execution(text)
        and not is_mcp_catalog_lookup(text)
        and _EXPLICIT_MCP_ACTION.search(action_text)
    )


def _entry_match_score(prompt: str, entry: MCPRegistryEntry, explicit: bool) -> int:
    text = prompt.casefold()
    names = (
        str(entry.display_name or ""),
        str(entry.server_name or ""),
        str(entry.provider_slug or ""),
    )
    score = max(
        (6 for name in names if name and name.casefold() in text),
        default=0,
    )
    slug = str(entry.provider_slug or "").casefold()
    terms = _PROVIDER_TERMS.get(slug, frozenset())
    if explicit and terms:
        score += 3 * sum(1 for term in terms if re.search(rf"\b{re.escape(term)}\b", text))
    if score:
        return score
    if explicit:
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in _KNOWN_CONNECTOR_TARGETS):
            return 0
        return 1
    # Do not let generic words such as "file" or "browser" route a normal
    # local request through an MCP process. The user must name the provider.
    return 0


def has_matching_connected_mcp(prompt: str, bindings: Mapping[str, Any]) -> bool:
    """Whether a connected MCP binding matches the provider requested in chat.

    Generic MCP requests can use any attached MCP. If the user names a
    service or a local capability, an unrelated attached server must not
    suppress the Connect prompt for the requested one.
    """
    text = " ".join(str(prompt or "").split()).casefold()
    if not bindings or not is_explicit_mcp_action_request(text):
        return False

    target_terms = _KNOWN_CONNECTOR_TARGETS | frozenset(
        term for terms in _PROVIDER_TERMS.values() for term in terms
    )
    has_named_target = any(re.search(rf"\b{re.escape(term)}\b", text) for term in target_terms)
    if not has_named_target:
        return True

    generic_slug_parts = {"local", "mcp", "server", "connector", "official"}
    for binding in bindings.values():
        server = getattr(binding, "server", None)
        names = (
            str(getattr(server, "name", "") or ""),
            str(getattr(server, "provider_slug", "") or ""),
        )
        if any(name and name.casefold() in text for name in names):
            return True
        slug = names[1].casefold()
        slug_parts = {
            part
            for part in re.split(r"[^a-z0-9]+", slug)
            if len(part) > 2 and part not in generic_slug_parts
        }
        provider_terms = _PROVIDER_TERMS.get(slug, frozenset()) | frozenset(slug_parts)
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in provider_terms):
            return True
    return False


def missing_stdio_mcp_options(
    db: Session,
    *,
    auth: AuthContext,
    prompt: str,
    connected: bool,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return matching, unconnected, approved STDIO launch choices.

    A normal local task stays on native tools. This prompt is only produced
    for explicit MCP actions or when the user names a catalog provider.
    """
    text = " ".join(prompt.split())
    if connected or not text or not requires_tool_execution(text) or is_mcp_catalog_lookup(text):
        return []
    explicit = is_explicit_mcp_action_request(text)
    named_connector = any(
        re.search(rf"\b{re.escape(target)}\b", text, re.IGNORECASE)
        for target in _IMPLICIT_CONNECTOR_TARGETS
    )
    # Most tool-using prompts are ordinary local work. Avoid querying the MCP
    # catalog unless the user explicitly asked for MCP or named a connector;
    # generic words such as "workspace", "files", or "GPU" should stay on
    # DeepSpace's native tools and must not trigger a connection prompt.
    if not explicit and not named_connector:
        return []
    if not explicit and excludes_mcp_for_prompt(text, {}):
        return []

    existing_ids = set(
        db.execute(
            select(MCPServer.registry_entry_id).where(
                MCPServer.tenant_id == auth.tenant_id,
                MCPServer.user_id == auth.user_id,
                MCPServer.registry_entry_id.is_not(None),
                MCPServer.enabled.is_(True),
                MCPServer.status == "connected",
            )
        ).scalars()
    )
    entries = (
        db.execute(
            select(MCPRegistryEntry)
            .where(
                MCPRegistryEntry.trust_status == "approved",
                MCPRegistryEntry.transport == "stdio",
            )
            .order_by(
                MCPRegistryEntry.popularity_rank.asc().nulls_last(), MCPRegistryEntry.display_name
            )
            .limit(200)
        )
        .scalars()
        .all()
    )
    matches: list[tuple[int, MCPRegistryEntry, dict[str, Any]]] = []
    for entry in entries:
        if entry.id in existing_ids:
            continue
        launch = stdio_launch_details(entry)
        score = _entry_match_score(text, entry, explicit)
        if launch is not None and score:
            matches.append((score, entry, launch))
    matches.sort(key=lambda item: (-item[0], item[1].display_name.casefold()))
    return [
        {
            "entry_id": str(entry.id),
            "name": entry.display_name,
            "description": str(entry.description or "")[:600],
            "command": launch["command"],
            "args": launch["args"],
            "permission_notice": (
                "This process runs on the host where the MCP worker executes, under that "
                "worker's operating-system account. In the desktop app, this is the local "
                "desktop account. It can use the files and network that account can access; "
                "DeepSpace's shell sandbox does not contain this MCP process."
            ),
        }
        for _score, entry, launch in matches[: max(1, min(int(limit), 5))]
    ]
