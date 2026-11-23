"""Fixture loader - loads recorded tool-calling fixtures for replay."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..lm_studio_client import ToolCallTrace
from ..scenario_library import Scenario


@dataclass
class LoadedFixture:
    """A loaded fixture with its trace and scenario metadata."""

    trace: ToolCallTrace
    scenario: Scenario
    category: str
    file_path: str
    line_number: int


class FixtureLoader:
    """Loads and parses fixture files for replay."""

    def __init__(self, fixture_dir: str = "fixtures"):
        self.fixture_dir = Path(fixture_dir)

    def load_category(self, category: str) -> list[LoadedFixture]:
        """Load all fixtures from a category file."""
        fixture_file = self.fixture_dir / f"{category}_tools.jsonl"

        if not fixture_file.exists():
            return []

        fixtures = []
        with open(fixture_file, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)

                trace = self._dict_to_trace(data)
                scenario = self._dict_to_scenario(data)

                fixtures.append(
                    LoadedFixture(
                        trace=trace,
                        scenario=scenario,
                        category=category,
                        file_path=str(fixture_file),
                        line_number=line_num,
                    )
                )

        return fixtures

    def load_all(self) -> dict[str, list[LoadedFixture]]:
        """Load all fixture categories."""
        result = {}
        for fixture_file in self.fixture_dir.glob("*_tools.jsonl"):
            category = fixture_file.stem.replace("_tools", "")
            result[category] = self.load_category(category)
        return result

    def load_scenario(self, scenario_id: str) -> LoadedFixture | None:
        """Load a specific scenario by ID."""
        all_fixtures = self.load_all()
        for _category, fixtures in all_fixtures.items():
            for fixture in fixtures:
                if fixture.scenario.id == scenario_id:
                    return fixture
        return None

    def _dict_to_trace(self, data: dict) -> ToolCallTrace:
        """Convert dict to ToolCallTrace."""
        return ToolCallTrace(
            scenario_id=data["scenario_id"],
            prompt=data["prompt"],
            tools_available=data["tools_available"],
            turns=data.get("turns", []),
            tool_calls=data.get("tool_calls", []),
            tool_results=data.get("tool_results", []),
            final_response=data.get("final_response"),
            errors=data.get("errors", []),
            started_at=data.get("started_at", 0),
            completed_at=data.get("completed_at"),
            metadata=data.get("metadata", {}),
        )

    def _dict_to_scenario(self, data: dict) -> Scenario:
        """Convert dict to Scenario (from metadata)."""
        metadata = data.get("metadata", {})
        return Scenario(
            id=data["scenario_id"],
            category=metadata.get("scenario_category", "unknown"),
            priority=metadata.get("scenario_priority", 2),
            prompt=data["prompt"],
            expected_tools=metadata.get("expected_tools", []),
            min_tool_calls=metadata.get("min_tool_calls", 1),
            max_tool_calls=metadata.get("max_tool_calls", 10),
            max_turns=metadata.get("max_turns", 10),
            assertions=metadata.get("assertions", {}),
            tags=metadata.get("tags", []),
            description=metadata.get("description", ""),
        )

    def get_fixture_stats(self) -> dict[str, Any]:
        """Get statistics about loaded fixtures."""
        all_fixtures = self.load_all()

        stats = {
            "categories": {},
            "total_fixtures": 0,
            "total_tool_calls": 0,
            "total_turns": 0,
            "total_errors": 0,
        }

        for category, fixtures in all_fixtures.items():
            cat_tool_calls = sum(len(f.trace.tool_calls) for f in fixtures)
            cat_turns = sum(len(f.trace.turns) for f in fixtures)
            cat_errors = sum(len(f.trace.errors) for f in fixtures)

            stats["categories"][category] = {
                "fixture_count": len(fixtures),
                "tool_calls": cat_tool_calls,
                "turns": cat_turns,
                "errors": cat_errors,
                "scenarios": [f.scenario.id for f in fixtures],
            }

            stats["total_fixtures"] += len(fixtures)
            stats["total_tool_calls"] += cat_tool_calls
            stats["total_turns"] += cat_turns
            stats["total_errors"] += cat_errors

        return stats
