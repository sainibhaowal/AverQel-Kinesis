"""Compatibility broker for the legacy native tool catalogue endpoint.

Current chat turns receive the authorized native tool schemas directly. This
broker remains available for compatible internal callers, and its results are
still restricted to the effective request allowlist.
"""

from __future__ import annotations

from typing import Any

from app.deepspace.services.tool_registry import ToolRegistry

DISCOVER_TOOLS_NAME = "discover_tools"
MAX_DISCOVERED_TOOLS = 8
MAX_CATALOG_TOOLS = 40
# Keep the core conversation controls on the first page of a large allowlist.
# The ordering is fixed, so discovery remains deterministic and does not infer
# authorization from the model's wording.
CATALOG_PRIORITY_NAMES = ("goal_read", "goal_set")

DISCOVER_TOOLS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DISCOVER_TOOLS_NAME,
        "description": (
            "Legacy compatibility catalogue lookup. Current DeepSpace turns receive authorized "
            "native tool schemas directly; do not call this to look up native tools. Internal "
            "callers may use it to inspect an authorized catalogue or load exact schemas."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 1000,
                    "description": (
                        "Required on every call, including the second schema-loading call. "
                        "State the capability or next action needed naturally; repeat the same "
                        "query when supplying tool_names."
                    ),
                },
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 80},
                    "minItems": 1,
                    "maxItems": MAX_DISCOVERED_TOOLS,
                    "description": (
                        "On the second call, exact capability names copied from the returned catalog. "
                        "Omit this on the first call."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100_000,
                    "description": (
                        "For catalog pages only: repeat the same query using next_offset from the "
                        "previous catalog response. Omit this on the first page and when loading schemas."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


class NativeToolBroker:
    """Page through the compact catalog and load exact authorized schemas."""

    def discover(
        self,
        *,
        query: str,
        registry: ToolRegistry,
        allowed_names: set[str],
        tool_names: list[str] | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List authorized tools or load explicitly selected schemas.

        ``allowed_names`` is assembled by the chat runtime for this request.
        The broker cannot load a name outside that set, even if a provider
        invents one or a future registry entry is added without exposure.
        Catalog pages keep the core goal controls first, then preserve
        deterministic ordering for every other authorized tool.
        """

        normalized_query = " ".join(str(query or "").split())[:1000]
        authorized_names = sorted(
            name
            for name in allowed_names
            if name != DISCOVER_TOOLS_NAME and registry.contains(name)
        )
        if tool_names is None:
            catalog_names = [name for name in CATALOG_PRIORITY_NAMES if name in authorized_names]
            catalog_names.extend(name for name in authorized_names if name not in catalog_names)
            page_offset = min(max(0, int(offset)), len(catalog_names))
            next_offset = (
                page_offset + MAX_CATALOG_TOOLS
                if page_offset + MAX_CATALOG_TOOLS < len(catalog_names)
                else None
            )
            catalog = [
                {
                    "name": name,
                    "description": registry.spec(name).description[:160],
                }
                for name in catalog_names[page_offset : page_offset + MAX_CATALOG_TOOLS]
            ]
            return {
                "status": "catalog",
                "requested_capability": normalized_query,
                "offset": page_offset,
                "available_tools": catalog,
                "next_offset": next_offset,
                "catalog_truncated": next_offset is not None,
                "message": (
                    f"If the needed capability is not listed, call discover_tools again with the "
                    f"same query and offset={next_offset}. No schema was loaded or action executed yet."
                    if next_offset is not None
                    else (
                        "There are no more authorized tools on this page. No schema was loaded or "
                        "action executed yet."
                        if not catalog
                        else "Choose exact capability names from available_tools, then call "
                        "discover_tools again with those names in tool_names. No schema was loaded "
                        "or action executed yet."
                    )
                ),
            }

        requested_names = list(dict.fromkeys(str(name).strip() for name in tool_names))
        invalid_names = [
            name
            for name in requested_names
            if name not in allowed_names or not registry.contains(name)
        ]
        if invalid_names:
            has_swarm_request = any(
                n
                in {
                    "spawn_teammate",
                    "wait_agent",
                    "team_task_create",
                    "team_task_update",
                    "team_task_list",
                }
                for n in invalid_names
            )
            guidance = (
                " Note: Swarm teammate delegation ('spawn_teammate') is not enabled in this session. "
                "In-process degradation active: Execute the planned tasks sequentially in-process using your direct tools."
                if has_swarm_request
                else ""
            )
            return {
                "status": "invalid_selection",
                "loaded_tools": [],
                "invalid_names": invalid_names[:MAX_DISCOVERED_TOOLS],
                "message": (
                    f"One or more names are not in this request's authorized native catalog.{guidance} "
                    "Review the catalog and select exact names from it."
                ),
            }
        if len(requested_names) > MAX_DISCOVERED_TOOLS:
            return {
                "status": "selection_too_large",
                "loaded_tools": [],
                "message": f"Select no more than {MAX_DISCOVERED_TOOLS} tools for one step.",
            }

        return {
            "status": "loaded",
            "loaded_tools": [
                {
                    "name": name,
                    "description": registry.spec(name).description[:240],
                }
                for name in requested_names
            ],
            "message": "The selected tool schemas are available on the next provider round.",
        }
