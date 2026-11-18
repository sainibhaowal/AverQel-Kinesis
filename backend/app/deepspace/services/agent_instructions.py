"""Agent instructions loader for DeepSpace.

Loads AGENTS.md, CLAUDE.md, and other instruction files as workspace context.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Instruction file names in priority order
INSTRUCTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "AGENT_INSTRUCTIONS.md",
    "INSTRUCTIONS.md",
    "agent-instructions.md",
    "claude.md",
)

MAX_INSTRUCTION_CHARS = 50_000


class AgentInstructionsLoader:
    """Loads and caches agent instruction files from the workspace."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, str]] = {}  # path -> (mtime, content)

    def load_instructions(self, workspace_root: Path) -> str | None:
        """Load the first available instruction file from the workspace.

        Returns:
            The instruction content, or None if no file found.
        """
        for filename in INSTRUCTION_FILES:
            path = workspace_root / filename
            if not path.is_file():
                continue

            try:
                mtime = path.stat().st_mtime
                cached = self._cache.get(str(path))
                if cached and cached[0] == mtime:
                    return cached[1]

                content = path.read_text(encoding="utf-8", errors="replace")
                if len(content) > MAX_INSTRUCTION_CHARS:
                    content = content[:MAX_INSTRUCTION_CHARS] + "\n\n[... truncated ...]"

                self._cache[str(path)] = (mtime, content)
                logger.info("Loaded agent instructions from %s", path)
                return content

            except Exception as e:
                logger.warning("Failed to load instructions from %s: %s", path, e)
                continue

        return None

    def clear_cache(self) -> None:
        """Clear the instruction cache."""
        self._cache.clear()


# Global loader instance
_instructions_loader = AgentInstructionsLoader()


def get_instructions_loader() -> AgentInstructionsLoader:
    """Get the global agent instructions loader."""
    return _instructions_loader


def load_workspace_instructions(workspace_root: Path) -> str | None:
    """Load agent instructions from workspace root."""
    return _instructions_loader.load_instructions(workspace_root)


def build_instructions_section(instructions: str | None) -> str | None:
    """Build the instructions section for system prompt."""
    if not instructions:
        return None

    return (
        "Workspace instructions (from AGENTS.md/CLAUDE.md):\n"
        "These instructions come from your workspace and define how the agent "
        "should behave in this project. They are project-specific guidance.\n\n"
        f"{instructions.strip()}"
    )


__all__ = [
    "AgentInstructionsLoader",
    "get_instructions_loader",
    "load_workspace_instructions",
    "build_instructions_section",
    "INSTRUCTION_FILES",
    "MAX_INSTRUCTION_CHARS",
]
