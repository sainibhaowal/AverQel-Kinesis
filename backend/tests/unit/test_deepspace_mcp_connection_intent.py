from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.deepspace.services.mcp_connection_intent import (
    has_matching_connected_mcp,
    is_explicit_mcp_action_request,
    missing_stdio_mcp_options,
)
from app.integrations.services.mcp_stdio_launch import stdio_launch_details

pytestmark = pytest.mark.unit_no_db


class _Scalars:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def __iter__(self):
        return iter(self.values)

    def all(self) -> list[object]:
        return self.values


class _Result:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self) -> _Scalars:
        return _Scalars(self.values)


class _Db:
    def __init__(self, entry: object, existing_ids: list[object] | None = None) -> None:
        self._results = [existing_ids or [], [entry]]

    def execute(self, _statement: object) -> _Result:
        return _Result(self._results.pop(0))


def _entry(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "transport": "stdio",
        "trust_status": "approved",
        "display_name": "Local Filesystem",
        "server_name": "local-filesystem",
        "provider_slug": "local-filesystem",
        "description": "Access files selected for this local MCP server.",
        "raw_metadata": {"server": {"command": "/usr/bin/python3", "args": ["-m", "local_mcp"]}},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_explicit_mcp_action_is_detected_but_explanation_is_not() -> None:
    assert is_explicit_mcp_action_request("Use MCP to inspect the workspace files.")
    assert is_explicit_mcp_action_request("Connect to the Spotify MCP and search my library.")
    assert not is_explicit_mcp_action_request("What is MCP STDIO used for?")
    assert not is_explicit_mcp_action_request("Use MCP to list tools only.")
    assert not is_explicit_mcp_action_request("Do not use MCP; check my GPU with bash.")
    assert not is_explicit_mcp_action_request(
        "Find sales-500.csv in my selected workspace. If you need to connect a local MCP, "
        "show me what will run and wait for me to choose Connect or Cancel."
    )


def test_stdio_launch_details_returns_exact_argv_and_rejects_invalid_metadata() -> None:
    entry = _entry()

    assert stdio_launch_details(entry) == {
        "command": "/usr/bin/python3",
        "args": ["-m", "local_mcp"],
    }
    assert stdio_launch_details(_entry(raw_metadata={"server": {"command": ""}})) is None
    assert (
        stdio_launch_details(
            _entry(raw_metadata={"server": {"command": "python", "args": ["bad\x00arg"]}})
        )
        is None
    )
    assert stdio_launch_details(_entry(transport="streamable_http")) is None


def test_missing_stdio_options_are_offered_only_for_explicit_mcp_actions() -> None:
    entry = _entry()
    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())

    options = missing_stdio_mcp_options(
        _Db(entry),
        auth=auth,
        prompt="Use MCP to inspect the workspace files.",
        connected=False,
    )

    assert len(options) == 1
    assert options[0]["entry_id"] == str(entry.id)
    assert options[0]["command"] == "/usr/bin/python3"
    assert options[0]["args"] == ["-m", "local_mcp"]
    assert "worker" in options[0]["permission_notice"].lower()
    assert not missing_stdio_mcp_options(
        _Db(entry),
        auth=auth,
        prompt="Check NVIDIA GPU status.",
        connected=False,
    )
    assert not missing_stdio_mcp_options(
        _Db(entry),
        auth=auth,
        prompt="Use MCP to search my Spotify library.",
        connected=False,
    )
    assert not missing_stdio_mcp_options(
        _Db(entry),
        auth=auth,
        prompt="Use MCP to inspect the workspace files.",
        connected=True,
    )


def test_workspace_search_with_conditional_mcp_disclosure_skips_catalog_lookup() -> None:
    class _UnexpectedDb:
        def execute(self, _statement: object) -> object:
            raise AssertionError("ordinary workspace search must not query MCP catalog")

    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())

    assert not missing_stdio_mcp_options(
        _UnexpectedDb(),
        auth=auth,
        prompt=(
            "Find sales-500.csv anywhere in my selected workspace. If you need to connect a "
            "local MCP, show me what will run and wait for me to choose Connect or Cancel."
        ),
        connected=False,
    )
    assert not missing_stdio_mcp_options(
        _UnexpectedDb(),
        auth=auth,
        prompt="Check GPU memory with the local shell.",
        connected=False,
    )


def test_missing_stdio_options_skip_existing_and_invalid_catalog_entries() -> None:
    entry = _entry()
    auth = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    prompt = "Use MCP to inspect the workspace files."

    assert not missing_stdio_mcp_options(
        _Db(entry, [entry.id]), auth=auth, prompt=prompt, connected=False
    )
    invalid = _entry(raw_metadata={"server": {"command": ""}})
    assert not missing_stdio_mcp_options(_Db(invalid), auth=auth, prompt=prompt, connected=False)


def test_unrelated_connected_mcp_does_not_satisfy_a_named_service_request() -> None:
    filesystem = SimpleNamespace(
        server=SimpleNamespace(name="Local Filesystem", provider_slug="local-filesystem")
    )
    spotify = SimpleNamespace(
        server=SimpleNamespace(name="Spotify", provider_slug="composio-spotify")
    )

    assert not has_matching_connected_mcp(
        "Use MCP to search my Spotify library.", {"filesystem_tool": filesystem}
    )
    assert has_matching_connected_mcp(
        "Use MCP to search my Spotify library.", {"spotify_tool": spotify}
    )
    assert has_matching_connected_mcp(
        "Use MCP to search the connected records.", {"filesystem_tool": filesystem}
    )
