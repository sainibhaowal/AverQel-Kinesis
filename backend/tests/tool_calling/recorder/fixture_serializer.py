"""Fixture serializer - handles JSONL fixture storage and loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..lm_studio_client import ToolCallTrace


@dataclass
class FixtureMetadata:
    """Metadata about a fixture file."""

    category: str
    scenario_count: int
    total_tool_calls: int
    total_turns: int
    recorded_at: float
    model: str
    lm_studio_url: str
    schema_version: str = "1.0"


class FixtureSerializer:
    """Serializes and deserializes tool-calling fixtures."""

    def __init__(self, fixture_dir: str = "fixtures"):
        self.fixture_dir = Path(fixture_dir)
        self.fixture_dir.mkdir(parents=True, exist_ok=True)

    def save_trace(self, trace: ToolCallTrace, category: str = "core") -> str:
        """Save a single trace to the appropriate category file."""
        fixture_file = self.fixture_dir / f"{category}_tools.jsonl"

        with open(fixture_file, "a", encoding="utf-8") as f:
            f.write(trace.to_jsonl() + "\n")

        return str(fixture_file)

    def save_traces(self, traces: list[ToolCallTrace], category: str = "core") -> str:
        """Save multiple traces."""
        fixture_file = self.fixture_dir / f"{category}_tools.jsonl"

        with open(fixture_file, "a", encoding="utf-8") as f:
            for trace in traces:
                f.write(trace.to_jsonl() + "\n")

        return str(fixture_file)

    def load_category(self, category: str) -> list[ToolCallTrace]:
        """Load all traces from a category file."""
        fixture_file = self.fixture_dir / f"{category}_tools.jsonl"

        if not fixture_file.exists():
            return []

        traces = []
        with open(fixture_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                traces.append(self._dict_to_trace(data))

        return traces

    def load_all(self) -> dict[str, list[ToolCallTrace]]:
        """Load all fixture categories."""
        result = {}
        for fixture_file in self.fixture_dir.glob("*_tools.jsonl"):
            category = fixture_file.stem.replace("_tools", "")
            result[category] = self.load_category(category)
        return result

    def _dict_to_trace(self, data: dict) -> ToolCallTrace:
        """Convert dict back to ToolCallTrace."""
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

    def get_category_stats(self, category: str) -> dict[str, Any]:
        """Get statistics for a fixture category."""
        traces = self.load_category(category)

        if not traces:
            return {"scenario_count": 0}

        total_tool_calls = sum(len(t.tool_calls) for t in traces)
        total_turns = sum(len(t.turns) for t in traces)
        total_errors = sum(len(t.errors) for t in traces)

        # Tool usage stats
        tool_usage = {}
        for t in traces:
            for tc in t.tool_calls:
                tool = tc.get("tool_name", "unknown")
                tool_usage[tool] = tool_usage.get(tool, 0) + 1

        return {
            "category": category,
            "scenario_count": len(traces),
            "total_tool_calls": total_tool_calls,
            "total_turns": total_turns,
            "total_errors": total_errors,
            "avg_tool_calls_per_scenario": total_tool_calls / len(traces),
            "avg_turns_per_scenario": total_turns / len(traces),
            "tool_usage": tool_usage,
            "scenarios": [t.scenario_id for t in traces],
        }

    def export_summary(self, output_path: str | None = None) -> dict[str, Any]:
        """Export a summary of all fixtures."""
        all_traces = self.load_all()

        summary = {
            "schema_version": "1.0",
            "categories": {},
            "total_scenarios": 0,
            "total_tool_calls": 0,
            "total_turns": 0,
            "tool_coverage": {},
        }

        for category, traces in all_traces.items():
            stats = self.get_category_stats(category)
            summary["categories"][category] = stats
            summary["total_scenarios"] += stats["scenario_count"]
            summary["total_tool_calls"] += stats["total_tool_calls"]
            summary["total_turns"] += stats["total_turns"]

            # Aggregate tool coverage
            for tool, count in stats["tool_usage"].items():
                summary["tool_coverage"][tool] = summary["tool_coverage"].get(tool, 0) + count

        if output_path:
            with open(output_path, "w") as f:
                json.dump(summary, f, indent=2, default=str)

        return summary

    def validate_fixtures(self) -> dict[str, Any]:
        """Validate fixture integrity."""
        issues = []
        all_traces = self.load_all()

        for category, traces in all_traces.items():
            for trace in traces:
                # Check required fields
                if not trace.scenario_id:
                    issues.append(f"{category}: missing scenario_id")
                if not trace.prompt:
                    issues.append(f"{trace.scenario_id}: missing prompt")
                if not trace.tools_available:
                    issues.append(f"{trace.scenario_id}: missing tools_available")

                # Check trace consistency
                for i, tc in enumerate(trace.tool_calls):
                    if "tool_call_id" not in tc:
                        issues.append(f"{trace.scenario_id}: tool_call {i} missing tool_call_id")
                    if "tool_name" not in tc:
                        issues.append(f"{trace.scenario_id}: tool_call {i} missing tool_name")

                # Check tool_results match tool_calls
                call_ids = {tc["tool_call_id"] for tc in trace.tool_calls}
                result_ids = {tr["tool_call_id"] for tr in trace.tool_results}
                missing_results = call_ids - result_ids
                if missing_results:
                    issues.append(f"{trace.scenario_id}: missing results for {missing_results}")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "categories_checked": len(all_traces),
            "total_traces": sum(len(t) for t in all_traces.values()),
        }


def load_fixtures(fixture_dir: str = "fixtures") -> dict[str, list[ToolCallTrace]]:
    """Convenience function to load all fixtures."""
    serializer = FixtureSerializer(fixture_dir)
    return serializer.load_all()


def save_fixtures(traces: list[ToolCallTrace], category: str, fixture_dir: str = "fixtures") -> str:
    """Convenience function to save fixtures."""
    serializer = FixtureSerializer(fixture_dir)
    return serializer.save_traces(traces, category)
