"""File reference system prompt section for DeepSpace.

Provides guidance to the model on how to reference files in the workspace.
"""

from __future__ import annotations

from typing import Any


def file_reference_system_prompt(settings: Any) -> str | None:
    """Render the file reference guidance as a system-prompt section.

    The section explains how to reference files in the workspace using the
    present tool and file references.

    Args:
        settings: The deployment settings in force.

    Returns:
        The section text, or None when the feature is disabled.
    """
    enabled = bool(getattr(settings, "deepspace_file_references_enabled", True))
    if not enabled:
        return None

    return (
        "File reference guidance. When you need to refer to a file in the workspace, "
        "you have three options:\n\n"
        "1. **Inline reference**: Mention the file path naturally in your response. "
        'The user can click the path to open it. Example: "The main logic is in '
        '`src/main.py`."\n\n'
        "2. **Deliverable declaration**: Use the `present` tool to formally declare "
        "files as deliverables. This creates a persistent record that appears in the "
        "deliverables panel. Use this for files the user needs to download, review "
        'separately, or share. Example: `present(paths=["report.pdf", "data.csv"])`.\n\n'
        "3. **@file references**: Use `@path/to/file` syntax to explicitly reference "
        "a file. This creates a clickable link and tells the system to include the "
        'file in context. Example: "See `@src/main.py` for the entry point."\n\n'
        "File references in your responses are automatically linked to the workspace "
        "file viewer. Paths must be workspace-relative (e.g., `src/main.py`, not "
        "`/home/user/project/src/main.py`).\n\n"
        "When using `@file` references, the system will fuzzy-match the path against "
        "the workspace index. Use enough path components to uniquely identify the file. "
        "Examples: `@main.py`, `@src/main.py`, `@tests/test_main.py`."
    )


def file_reference_system_prompt_section(settings: Any) -> str | None:
    """Render the file reference guidance as a system-prompt section.

    Alias for file_reference_system_prompt for consistency with other sections.

    Args:
        settings: The deployment settings in force.

    Returns:
        The section text, or None when the feature is disabled.
    """
    return file_reference_system_prompt(settings)


__all__ = [
    "file_reference_system_prompt",
    "file_reference_system_prompt_section",
]
