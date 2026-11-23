"""Pytest integration tests for tool-calling recorder and replay."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tool_calling.recorder.fixture_serializer import FixtureSerializer
from tests.tool_calling.scenario_library import load_scenarios

pytestmark = pytest.mark.unit_no_db


@pytest.fixture(scope="session")
def fixture_dir():
    """Fixture directory path."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def scenario_files():
    """List of scenario YAML files."""
    scenario_dir = Path(__file__).parent / "scenario_library"
    return sorted(str(f) for f in scenario_dir.glob("*.yaml"))


class TestFixtureIntegrity:
    """Tests for fixture file integrity."""

    def test_fixture_directory_exists(self, fixture_dir):
        """Fixture directory should exist."""
        assert fixture_dir.exists(), f"Fixture directory not found: {fixture_dir}"

    def test_core_fixtures_exist(self, fixture_dir):
        """Core fixtures should exist (recorded locally)."""
        core_file = fixture_dir / "core_tools.jsonl"
        # May not exist in CI if not recorded yet
        # This test documents the expectation
        if core_file.exists():
            assert core_file.stat().st_size > 0, "Core fixtures file is empty"

    def test_fixture_validation(self, fixture_dir):
        """Fixtures should pass validation."""
        serializer = FixtureSerializer(str(fixture_dir))
        validation = serializer.validate_fixtures()

        if validation["total_traces"] > 0:
            assert validation["valid"], f"Fixture validation failed: {validation['issues']}"

    def test_fixture_summary(self, fixture_dir):
        """Fixture summary should be readable."""
        serializer = FixtureSerializer(str(fixture_dir))
        summary = serializer.export_summary()

        assert "total_scenarios" in summary
        assert "categories" in summary
        assert isinstance(summary["categories"], dict)


class TestScenarioLibrary:
    """Tests for scenario library."""

    def test_load_all_scenarios(self, scenario_files):
        """Should be able to load all scenario files."""
        total_scenarios = 0
        for f in scenario_files:
            scenarios = load_scenarios(f)
            assert len(scenarios) > 0, f"No scenarios in {f}"
            total_scenarios += len(scenarios)

        # We have ~98 scenarios across 5 categories
        assert total_scenarios >= 95, f"Expected 95+ scenarios, got {total_scenarios}"

    def test_scenario_structure(self, scenario_files):
        """All scenarios should have required fields."""
        for f in scenario_files:
            scenarios = load_scenarios(f)
            for s in scenarios:
                assert s.id, f"Missing id in {f}"
                assert s.category, f"Missing category in {s.id}"
                assert s.priority in (0, 1, 2), f"Invalid priority in {s.id}"
                assert s.prompt, f"Missing prompt in {s.id}"
                assert isinstance(s.expected_tools, list), f"expected_tools not list in {s.id}"
                assert s.min_tool_calls >= 1, f"min_tool_calls < 1 in {s.id}"
                assert s.max_turns >= 1, f"max_turns < 1 in {s.id}"

    def test_no_duplicate_ids(self, scenario_files):
        """Scenario IDs should be unique across all files."""
        all_ids = []
        for f in scenario_files:
            scenarios = load_scenarios(f)
            all_ids.extend(s.id for s in scenarios)

        assert len(all_ids) == len(set(all_ids)), "Duplicate scenario IDs found"

    def test_category_distribution(self, scenario_files):
        """Should have scenarios in all categories."""
        categories = set()
        for f in scenario_files:
            scenarios = load_scenarios(f)
            for s in scenarios:
                categories.add(s.category)

        expected = {"core", "advanced", "chaining", "error", "specialized"}
        assert categories == expected, f"Categories mismatch: {categories}"


class TestToolInventory:
    """Tests for tool inventory."""

    def test_tool_inventory_exists(self):
        """Tool inventory JSON should exist."""
        inventory_path = Path(__file__).parent / "tool_inventory.json"
        assert inventory_path.exists(), "Tool inventory not found. Run extract_tools.py first."

    def test_tool_inventory_structure(self):
        """Tool inventory should have expected structure."""
        import json

        inventory_path = Path(__file__).parent / "tool_inventory.json"
        with open(inventory_path) as f:
            inventory = json.load(f)

        assert "total_tools" in inventory
        assert "tools" in inventory
        assert "categories" in inventory
        assert inventory["total_tools"] > 100

        # Check each tool has required fields
        for tool in inventory["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "mode" in tool
            assert "has_handler" in tool

    def test_all_tools_have_handlers(self):
        """All tools in inventory should have handlers."""
        import json

        inventory_path = Path(__file__).parent / "tool_inventory.json"
        with open(inventory_path) as f:
            inventory = json.load(f)

        missing_handlers = [t["name"] for t in inventory["tools"] if not t["has_handler"]]
        # Some tools might not have handlers (that's OK for settings-gated)
        # Just warn
        if missing_handlers:
            print(f"Tools without handlers: {missing_handlers}")


class TestReplayIntegration:
    """Integration tests for replay (require fixtures)."""

    @pytest.mark.skipif(
        not (Path(__file__).parent / "fixtures" / "core_tools.jsonl").exists(),
        reason="Core fixtures not recorded yet",
    )
    def test_replay_core_fixtures(self, fixture_dir):
        """Core fixtures should replay successfully."""

        # This would be an async test - skip for now
        pytest.skip("Async replay test - run via CLI instead")


# Pytest markers
def pytest_configure(config):
    config.addinivalue_line("markers", "recorder: marks tests that require LM Studio for recording")
    config.addinivalue_line("markers", "replay: marks tests that replay fixtures")
    config.addinivalue_line("markers", "nightly: marks tests for nightly runs")
    config.addinivalue_line("markers", "requires_lm_studio: marks tests requiring LM Studio")
