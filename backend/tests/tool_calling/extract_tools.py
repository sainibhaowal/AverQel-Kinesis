#!/usr/bin/env python3
"""
Extract all DeepSpace native tools from the tool registry.
Run this to generate the complete tool inventory for test scenarios.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.deepspace.services.tool_registry import build_native_tool_registry
from app.deepspace.services.tools import (
    NATIVE_TOOL_ARGUMENT_MODELS,
    NATIVE_TOOL_DEADLINES,
    NATIVE_TOOL_DEFINITIONS,
    NATIVE_TOOL_HANDLERS,
    NATIVE_TOOL_MODES,
    NATIVE_TOOL_SPECS,
    SETTINGS_GATED_TOOLS,
)

# The 15 additional tools defined in chat_service.py (not in NATIVE_TOOL_SPECS)
CHAT_SERVICE_EXTRA_TOOLS = {
    "discover_tools": {
        "description": "Legacy compatibility catalogue lookup. Current DeepSpace turns should call the specific tools they need directly.",
        "mode": "read",
    },
    "web_search": {
        "description": "Search the public web through the configured self-hosted search provider. Use this for current, time-sensitive, unfamiliar, or source-backed information.",
        "mode": "read",
    },
    "get_tool_result": {
        "description": "Retrieve a bounded continuation of a previously referenced native tool result. Use only with a result_ref returned in this conversation.",
        "mode": "read",
    },
    "url_read": {
        "description": "Read a public HTTPS web URL for source-backed research. Prefer this on URLs returned by web_search so the answer is based on fetched page text, not only a snippet.",
        "mode": "read",
    },
    "image_read": {
        "description": "Inspect a public raster or SVG image URL for dimensions and metadata. Use this when the user asks about an image at a URL.",
        "mode": "read",
    },
    "sandbox_execute": {
        "description": "Run bounded Python, read-only SQL, Bash, or PowerShell in a sandbox. Returns stdout, stderr, exit code, and any generated files.",
        "mode": "write",
    },
    "artifact_create": {
        "description": "Persist a structured report, table, or data artifact in the conversation. Use for structured outputs like CSV, JSON, Markdown, HTML, SVG, Mermaid, or UML that the user can reference later.",
        "mode": "write",
    },
    "ask_user": {
        "description": "Ask the user for information that is genuinely required before proceeding. Returns the user's response or a timeout indication.",
        "mode": "interactive",
    },
    "todo_write": {
        "description": "Manage the conversation's task plan. Supply a todos list to create/replace the plan, or an operation with task_id to update a single task's status with evidence.",
        "mode": "write",
    },
    "analyze": {
        "description": "Compare two or more authorized Library documents by their extracted text. Returns a structured diff with aligned passages and a summary.",
        "mode": "read",
    },
    "read": {
        "description": "Read authorized DeepSpace data without changing it. For Library files, memories, notes, chats, projects, or workspace files.",
        "mode": "read",
    },
    "find": {
        "description": "Find authorized Library files, memories, or chat messages by query. Returns matching items with snippets.",
        "mode": "read",
    },
    "write": {
        "description": "Create or update authorized DeepSpace content. Choose exactly one target: note, library, or memory.",
        "mode": "write",
    },
    "edit": {
        "description": "Modify an authorized note, Library file, or project workspace file. Supports replace, append, rename, and move operations.",
        "mode": "write",
    },
    "delete": {
        "description": "Delete one authorized Library file, memory, or project workspace file. Use only when the user explicitly requests deletion.",
        "mode": "write",
    },
}


def extract_tool_inventory() -> dict:
    """Extract complete tool inventory with all metadata from both sources."""

    # Build the registry from NATIVE_TOOL_DEFINITIONS (same as chat_service.py)
    extra_contracts = {
        name: (model, NATIVE_TOOL_MODES[name])
        for name, model in NATIVE_TOOL_ARGUMENT_MODELS.items()
        if name in NATIVE_TOOL_MODES
    }
    registry = build_native_tool_registry(
        NATIVE_TOOL_DEFINITIONS,
        extra_contracts=extra_contracts,
    )

    tools = []

    # 1. Tools from NATIVE_TOOL_SPECS (133 tools)
    for spec in NATIVE_TOOL_SPECS:
        name = spec.name
        provider_def = registry.emit(name) if registry.contains(name) else spec.definition
        func_def = provider_def.get("function", {}) if isinstance(provider_def, dict) else {}

        tool_info = {
            "name": name,
            "description": func_def.get("description", ""),
            "mode": spec.mode.value if hasattr(spec.mode, "value") else str(spec.mode),
            "concurrency": getattr(spec, "concurrency", "read_parallel"),
            "idempotency": getattr(spec, "idempotency", "safe"),
            "schema_digest": getattr(spec, "schema_digest", ""),
            "deadline_seconds": NATIVE_TOOL_DEADLINES.get(name),
            "settings_gated": name in SETTINGS_GATED_TOOLS,
            "gate_setting": SETTINGS_GATED_TOOLS.get(name),
            "provider_definition": provider_def,
            "argument_model": (
                NATIVE_TOOL_ARGUMENT_MODELS.get(name).__name__
                if name in NATIVE_TOOL_ARGUMENT_MODELS
                else None
            ),
            "has_handler": name in NATIVE_TOOL_HANDLERS,
            "source": "native_tool_specs",
        }
        tools.append(tool_info)

    # 2. Additional tools from chat_service.py (15 tools)
    for name, extra_info in CHAT_SERVICE_EXTRA_TOOLS.items():
        # These tools ARE in the registry (added via chat_service.py's NATIVE_TOOL_REGISTRY)
        # but NOT in NATIVE_TOOL_SPECS
        provider_def = registry.emit(name) if registry.contains(name) else None
        func_def = provider_def.get("function", {}) if isinstance(provider_def, dict) else {}

        tool_info = {
            "name": name,
            "description": func_def.get("description", extra_info["description"]),
            "mode": extra_info["mode"],
            "concurrency": (
                "read_parallel"
                if extra_info["mode"] == "read"
                else ("write_serial" if extra_info["mode"] == "write" else "interactive")
            ),
            "idempotency": (
                "safe"
                if extra_info["mode"] == "read"
                else ("guarded" if extra_info["mode"] == "write" else "external")
            ),
            "schema_digest": "",
            "deadline_seconds": None,
            "settings_gated": name in SETTINGS_GATED_TOOLS,
            "gate_setting": SETTINGS_GATED_TOOLS.get(name),
            "provider_definition": provider_def,
            "argument_model": (
                NATIVE_TOOL_ARGUMENT_MODELS.get(name).__name__
                if name in NATIVE_TOOL_ARGUMENT_MODELS
                else None
            ),
            "has_handler": name in NATIVE_TOOL_HANDLERS,
            "source": "chat_service_extra",
        }
        tools.append(tool_info)

    # Categorize by module
    categories = {}
    for spec in NATIVE_TOOL_SPECS:
        module_name = spec.__module__ if hasattr(spec, "__module__") else "unknown"
        if module_name not in categories:
            categories[module_name] = []
        categories[module_name].append(spec.name)

    # Add chat_service extra tools as their own category
    categories["app.deepspace.services.chat_service (extra)"] = list(
        CHAT_SERVICE_EXTRA_TOOLS.keys()
    )

    return {
        "total_tools": len(tools),
        "tools": tools,
        "categories": categories,
        "settings_gated_tools": SETTINGS_GATED_TOOLS,
        "registry_tool_names": list(registry.names()),
        "native_tool_specs_count": len(NATIVE_TOOL_SPECS),
        "chat_service_extra_count": len(CHAT_SERVICE_EXTRA_TOOLS),
    }


def main():
    inventory = extract_tool_inventory()

    # Save as JSON
    output_path = Path(__file__).parent / "tool_inventory.json"
    with open(output_path, "w") as f:
        json.dump(inventory, f, indent=2, default=str)

    print(f"Extracted {inventory['total_tools']} tools")
    print(f"  - From NATIVE_TOOL_SPECS: {inventory['native_tool_specs_count']}")
    print(f"  - From chat_service.py extra: {inventory['chat_service_extra_count']}")
    print(f"Saved to {output_path}")

    # Print summary
    print("\n=== Tool Categories ===")
    for cat, tools in sorted(inventory["categories"].items()):
        print(f"  {cat}: {len(tools)} tools")
        for t in sorted(tools):
            print(f"    - {t}")

    print(f"\nSettings-gated tools: {len(inventory['settings_gated_tools'])}")
    for tool, setting in sorted(inventory["settings_gated_tools"].items()):
        print(f"  {tool} -> {setting}")


if __name__ == "__main__":
    main()
