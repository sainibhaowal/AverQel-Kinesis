"""Tests for the workspace type graph.

The graph answers two questions a coding agent asks: where is this
defined, and what refers to it. These tests cover the reading of Python
and of the script languages, and the answers the graph gives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.deepspace.services.type_graph import (
    TypeGraph,
    build_type_graph,
    graph_fingerprint,
)

pytestmark = pytest.mark.unit_no_db


def _context(root: Path) -> Any:
    """Build a tool context whose workspace is one directory.

    Args:
        root: The workspace root.

    Returns:
        The context.
    """
    from types import SimpleNamespace

    workspace = root / "tenant" / "user"
    workspace.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        settings=SimpleNamespace(deepspace_workspace_root=str(root)),
        auth=SimpleNamespace(tenant_id="tenant", user_id="user"),
        db=None,
    )


def _write(root: Path, name: str, content: str) -> Path:
    """Write one source file into the workspace.

    Args:
        root: The workspace root.
        name: The file's name.
        content: The file's content.

    Returns:
        The file's path.
    """
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_a_python_class_and_its_methods_are_declared(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "service.py",
        "class Service:\n"
        "    def start(self):\n"
        "        return 1\n"
        "\n"
        "def helper():\n"
        "    return Service()\n",
    )

    graph = build_type_graph(tmp_path)

    assert "Service" in graph.symbols
    assert graph.symbols["Service"].kind == "class"
    assert "Service.start" in graph.symbols
    assert graph.symbols["Service.start"].kind == "method"
    assert "helper" in graph.symbols
    assert graph.symbols["helper"].kind == "function"


def test_a_reference_is_recorded_with_its_line(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "def main():\n    return helper()\n\ndef helper():\n    return 1\n",
    )

    graph = build_type_graph(tmp_path)

    references = graph.references_to("helper")
    assert references
    assert references[0].path == "app.py"
    assert references[0].line == 2


def test_a_definition_is_found_by_its_name(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "class Widget:\n    pass\n")
    _write(tmp_path, "b.py", "def render_widget():\n    return Widget()\n")

    graph = build_type_graph(tmp_path)

    definitions = graph.definitions_of("Widget")
    assert [definition.path for definition in definitions] == ["a.py"]
    assert graph.references_to("Widget")


def test_a_module_constant_is_declared(tmp_path: Path) -> None:
    _write(tmp_path, "settings.py", "MAX_ROWS = 100\n")

    graph = build_type_graph(tmp_path)

    assert graph.symbols["MAX_ROWS"].kind == "constant"


def test_a_typescript_class_and_interface_are_declared(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "widget.ts",
        "export interface WidgetProps {\n  id: string;\n}\n"
        "export class Widget {\n  render(): void {}\n}\n"
        "export type WidgetId = string;\n",
    )

    graph = build_type_graph(tmp_path)

    assert graph.symbols["WidgetProps"].kind == "interface"
    assert graph.symbols["Widget"].kind == "class"
    assert graph.symbols["WidgetId"].kind == "type"


def test_a_broken_file_does_not_stop_the_graph(tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", "def (:\n")
    _write(tmp_path, "good.py", "def fine():\n    return 1\n")

    graph = build_type_graph(tmp_path)

    assert "fine" in graph.symbols


def test_the_skipped_directories_are_not_read(tmp_path: Path) -> None:
    _write(tmp_path, "node_modules/pkg/index.js", "function skipped() {}\n")
    _write(tmp_path, ".git/hooks/pre-commit.py", "def skipped():\n    pass\n")
    _write(tmp_path, "kept.py", "def kept():\n    return 1\n")

    graph = build_type_graph(tmp_path)

    assert "kept" in graph.symbols
    assert "skipped" not in graph.symbols


def test_a_workspace_without_sources_has_an_empty_graph(tmp_path: Path) -> None:
    graph = build_type_graph(tmp_path)

    assert graph.symbols == {}
    assert graph.files == ()


def test_the_fingerprint_changes_when_the_declarations_change(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "def one():\n    return 1\n")
    before = graph_fingerprint(build_type_graph(tmp_path))

    _write(tmp_path, "a.py", "def one():\n    return 2\n\ndef two():\n    return 3\n")
    after = graph_fingerprint(build_type_graph(tmp_path))

    assert before != after


def test_the_graph_serializes_for_the_model(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "def one():\n    return 1\n")
    graph: TypeGraph = build_type_graph(tmp_path)

    payload = graph.as_dict()

    assert payload["symbols"]
    assert payload["symbols"][0]["name"] == "one"


def test_the_tool_answers_where_a_name_is_defined(tmp_path: Path) -> None:
    from app.deepspace.services.tools.type_graph import _type_graph

    context = _context(tmp_path)
    _write(
        Path(context.settings.deepspace_workspace_root) / "tenant" / "user",
        "a.py",
        "class Widget:\n    pass\n",
    )

    result = asyncio.run(_type_graph({"name": "Widget"}, context))

    assert result["status"] == "ok"
    assert result["definitions"]
    assert result["definitions"][0]["name"] == "Widget"


def test_the_tool_returns_the_whole_graph_without_a_name(tmp_path: Path) -> None:
    from app.deepspace.services.tools.type_graph import _type_graph

    context = _context(tmp_path)
    _write(
        Path(context.settings.deepspace_workspace_root) / "tenant" / "user",
        "a.py",
        "def one():\n    return 1\n",
    )

    result = asyncio.run(_type_graph({}, context))

    assert result["status"] == "ok"
    assert result["symbols"]
    assert result["fingerprint"]
    assert isinstance(result, dict)


def test_the_tool_works_without_a_database(tmp_path: Path) -> None:
    from app.deepspace.services.tools.type_graph import _type_graph

    result = asyncio.run(_type_graph({}, _context(tmp_path)))

    assert result["status"] == "ok"
    assert isinstance(result, dict)
    assert result["root"]


def _unused(value: Any) -> Any:
    """Return one value unchanged.

    Args:
        value: The value.

    Returns:
        The value.
    """
    return value
