"""Scenario library for tool-calling tests.

Each scenario defines:
- A user prompt that should trigger specific tool calls
- Expected tools to be called (for validation)
- Category and priority
- Optional setup/teardown
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class Scenario:
    """One tool-calling test scenario."""

    id: str
    category: str  # core, advanced, specialized, chaining, error
    priority: int  # 0=P0, 1=P1, 2=P2
    prompt: str
    expected_tools: list[str] = field(default_factory=list)
    min_tool_calls: int = 1
    max_tool_calls: int = 10
    max_turns: int = 10
    setup: dict[str, Any] = field(default_factory=dict)  # Pre-test setup (files, etc.)
    assertions: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    skip_in_ci: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "priority": self.priority,
            "prompt": self.prompt,
            "expected_tools": self.expected_tools,
            "min_tool_calls": self.min_tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_turns": self.max_turns,
            "setup": self.setup,
            "assertions": self.assertions,
            "tags": self.tags,
            "description": self.description,
            "skip_in_ci": self.skip_in_ci,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Scenario:
        return cls(**data)


def load_scenarios(path: str) -> list[Scenario]:
    """Load scenarios from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return [Scenario.from_dict(s) for s in data.get("scenarios", [])]


def save_scenarios(scenarios: list[Scenario], path: str) -> None:
    """Save scenarios to a YAML file."""
    with open(path, "w") as f:
        yaml.dump(
            {"scenarios": [s.to_dict() for s in scenarios]},
            f,
            default_flow_style=False,
            sort_keys=False,
        )
