"""Tests for requiring fixture coverage across the native tool inventory."""

import json
from pathlib import Path

import pytest

from tests.tool_calling import check_fixture_coverage
from tests.tool_calling.recorder.fixture_serializer import FixtureSerializer

pytestmark = pytest.mark.unit_no_db


def _write_inventory(path: Path, tool_names: list[str]) -> None:
    path.write_text(
        json.dumps({"tools": [{"name": name} for name in tool_names]}),
        encoding="utf-8",
    )


def test_fixture_coverage_fails_and_lists_uncovered_tools(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    inventory_path = tmp_path / "tool_inventory.json"
    _write_inventory(inventory_path, ["read", "write"])
    monkeypatch.setattr(
        FixtureSerializer,
        "export_summary",
        lambda self: {"tool_coverage": {"read": 1}},
    )

    status = check_fixture_coverage.verify_fixture_coverage(tmp_path / "fixtures", inventory_path)

    assert status == 1
    output = capsys.readouterr().out
    assert "Missing coverage: 1" in output
    assert "  - write" in output


def test_fixture_coverage_passes_when_every_inventory_tool_is_recorded(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    inventory_path = tmp_path / "tool_inventory.json"
    _write_inventory(inventory_path, ["read", "write"])
    monkeypatch.setattr(
        FixtureSerializer,
        "export_summary",
        lambda self: {"tool_coverage": {"read": 1, "write": 1}},
    )

    status = check_fixture_coverage.verify_fixture_coverage(tmp_path / "fixtures", inventory_path)

    assert status == 0
    assert "All inventoried tools have fixture coverage." in capsys.readouterr().out
