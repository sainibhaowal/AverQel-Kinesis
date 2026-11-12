"""Small, deterministic signals for requests that need a real tool result.

This is a routing safeguard, not a permission decision. The selected preset,
deployment policy, and execution-time checks still decide which tools can run.
"""

from __future__ import annotations

import re

_INSPECTION_ACTION = re.compile(
    r"\b(?:check|inspect|find|locate|list|read|open|scan|search|verify|"
    r"analy[sz]e|show|get|fetch|browse|download|upload|navigate|click|type|"
    r"use|call|run|execute|test|build|launch|start|stop|edit|write|create|"
    r"update|fix|modify|delete|remove|send|install|uninstall)\b",
    re.IGNORECASE,
)
_LIVE_STATUS_QUERY = re.compile(
    r"\b(?:status|current(?:ly)?|latest|now|today|installed|available|running|"
    r"configured|enabled|disabled|where\s+(?:is|are)|what\s+(?:is|are)\s+"
    r"(?:the\s+)?(?:current|latest|status|available|installed|running|configured)|"
    r"what\s+.+\b(?:available|installed|running|configured|enabled)\b)\b",
    re.IGNORECASE,
)
_LIVE_TARGET = re.compile(
    r"\b(?:file|files|folder|folders|directory|directories|path|paths|workspace|"
    r"repository|repo|codebase|project|logs?|process(?:es)?|service|services|port|"
    r"ports|gpu|nvidia|cpu|memory|disk|system|computer|laptop|terminal|command|"
    r"application|app|server|tests?|tool|tools|catalog(?:ue)?|provider|providers|"
    r"browser|website|web|online|source|sources|latest|current|today|now|weather|"
    r"temperature|price|rate|stock|crypto|news|email|inbox|message|calendar|"
    r"ticket|issue|database|api|shell|bash|pwsh|package|model|conversation|chat|"
    r"connected service|mcp)\b",
    re.IGNORECASE,
)
_EXPLICIT_PATH_OR_FILENAME = re.compile(
    r"(?:^|\s)(?:/[A-Za-z0-9._ -]+(?:/[A-Za-z0-9._ -]+)+|"
    r"[A-Za-z0-9._-]+\.(?:csv|tsv|json|ya?ml|toml|ini|log|md|txt|py|js|jsx|"
    r"ts|tsx|sh|ps1|sql|db|sqlite))\b",
    re.IGNORECASE,
)
_MCP_CATALOG_QUERY = re.compile(
    r"\b(?:available\s+tools?|tool\s+(?:catalog(?:ue)?|list|schemas?|definitions)|"
    r"(?:what|which)\s+(?:(?:connected|mcp)\s+)?tools?|"
    r"list\s+(?:the\s+)?(?:(?:connected|mcp)\s+)?tools?)\b|"
    r"\b(?:search|find|look\s+for)\b.{0,80}\b(?:mcp|connected)\b.{0,50}\btools?\b"
    r".{0,100}\b(?:a|an|the)\s+[a-z0-9-]+(?:\s+[a-z0-9-]+){0,3}\s+"
    r"(?:tool|function|endpoint|capability)\b",
    re.IGNORECASE,
)
_EXPLANATION_QUERY = re.compile(
    r"^(?:what\s+does\b.+\bmean\??|what\s+is\s+(?:the\s+)?"
    r"(?:meaning|definition)\s+of\b.+|explain\b|describe\b|"
    r"how\s+does\b.+\bwork\??)$",
    re.IGNORECASE,
)
_MCP_OPT_OUT = re.compile(
    r"\b(?:do\s+not|don't|dont|never|avoid)\s+"
    r"(?:use|call|search|query|access|route\s+through)\s+"
    r"(?:the\s+)?(?:mcp|connected\s+(?:tools?|services?))\b|"
    r"\bwithout\s+(?:using\s+)?(?:mcp|connected\s+(?:tools?|services?))\b|"
    r"\bnot\s+(?:via|through)\s+(?:the\s+)?mcp\b",
    re.IGNORECASE,
)
_EXPLICIT_MCP_ACTION = re.compile(
    r"\b(?:use|call|search|query|access|run|execute)\s+"
    r"(?:the\s+)?(?:mcp|connected\s+(?:tools?|services?))\b",
    re.IGNORECASE,
)
_LOCAL_TOOL_REQUEST = re.compile(
    r"\b(?:via|using|with)\s+(?:the\s+)?(?:bash|shell|terminal|command\s+line)\b|"
    r"\b(?:on|from)\s+(?:my\s+)?(?:laptop|computer|host|local\s+system)\b|"
    r"\b(?:local\s+filesystem|host\s+system|gpu\s+status|nvidia-smi|"
    r"(?:gpu|nvidia|cpu|disk)\s+(?:status|usage|memory|information))\b",
    re.IGNORECASE,
)
_SERVICE_INTENT_TERMS: dict[str, frozenset[str]] = {
    "gmail": frozenset({"email", "emails", "inbox", "mail", "mails"}),
    "outlook": frozenset({"email", "emails", "inbox", "mail", "mails"}),
    "calendar": frozenset(
        {"calendar", "event", "events", "meeting", "meetings", "appointment", "appointments"}
    ),
    "slack": frozenset({"slack", "channel", "channels", "thread", "threads"}),
    "drive": frozenset({"drive", "document", "documents", "spreadsheet", "spreadsheets"}),
}


def is_mcp_catalog_lookup(prompt: str | None) -> bool:
    """Return whether the request is about finding MCP tools, not calling one."""

    text = " ".join(str(prompt or "").split())
    return bool(_MCP_CATALOG_QUERY.search(text))


def requires_connected_service_action(prompt: str | None, bindings: object) -> bool:
    """Return whether a task needs an actual call to an attached MCP service.

    Catalog questions are handled by the broker's search/schema operations and
    must not be mistaken for a request to perform an action in the service.
    """

    text = " ".join(str(prompt or "").split())
    if not text or is_mcp_catalog_lookup(text) or not isinstance(bindings, dict) or not bindings:
        return False
    if _MCP_OPT_OUT.search(text) or _EXPLANATION_QUERY.match(text):
        return False
    lowered = text.casefold()
    if _EXPLICIT_MCP_ACTION.search(text):
        return True

    service_names: set[str] = set()
    for binding in bindings.values():
        server = getattr(binding, "server", None)
        name = (
            str(getattr(server, "name", "") or getattr(binding, "server_name", "") or "")
            .strip()
            .casefold()
        )
        if name:
            service_names.add(name)
    for name in service_names:
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return True
        words = [word for word in re.split(r"[^a-z0-9]+", name) if len(word) >= 3]
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
            return True
        # A small, explicit set of service-specific nouns preserves natural
        # requests such as "check my inbox" without letting an unrelated
        # attached connector capture local requests such as "check GPU status".
        service_terms = set().union(
            *(terms for service, terms in _SERVICE_INTENT_TERMS.items() if service in words)
        )
        if service_terms and any(
            re.search(rf"\b{re.escape(term)}\b", lowered) for term in service_terms
        ):
            return True

    # A connector being present does not mean a local request targets it.
    # Only a named service or explicit MCP instruction selects this route.
    return False


def excludes_mcp_for_prompt(prompt: str | None, bindings: object) -> bool:
    """Return whether this turn explicitly opts out of MCP or targets local tools."""

    text = " ".join(str(prompt or "").split())
    if not text or is_mcp_catalog_lookup(text):
        return False
    if _MCP_OPT_OUT.search(text):
        return True
    return bool(
        _LOCAL_TOOL_REQUEST.search(text) and not requires_connected_service_action(text, bindings)
    )


def requires_tool_execution(prompt: str | None) -> bool:
    """Return whether a request clearly asks to inspect or change live state.

    Ordinary explanations and creative requests remain optional-tool turns.
    This result only guides model routing and structured-call enforcement; it
    never widens the selected tool set or bypasses runtime permissions.
    """

    text = " ".join(str(prompt or "").split())
    if not text:
        return False
    # Hyphenated settings such as "workspace-write" contain action words,
    # but questions asking what a setting means are explanatory, not requests
    # to edit the workspace.
    if _EXPLANATION_QUERY.match(text):
        return False
    return bool(
        (_INSPECTION_ACTION.search(text) and _LIVE_TARGET.search(text))
        or (_INSPECTION_ACTION.search(text) and _EXPLICIT_PATH_OR_FILENAME.search(text))
        or (_LIVE_STATUS_QUERY.search(text) and _LIVE_TARGET.search(text))
    )
