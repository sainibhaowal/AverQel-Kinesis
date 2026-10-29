"""End-to-end tests for the Node.js programmatic-tool-calling runtime.

The tests start real Node processes inside bubblewrap and call host tools
through the stdout/stdin channel, so they cover the protocol, the cage, and
the tool ledger together. They skip when Node or bubblewrap is unavailable.
"""

from __future__ import annotations

import shutil

import pytest

from app.core.config import get_settings
from app.deepspace.services.ptc_runtime_node import NodePtcRuntime
from app.deepspace.services.sandbox_backend import BwrapBackend

pytestmark = pytest.mark.unit_no_db

requires_node = pytest.mark.skipif(
    shutil.which("node") is None or not BwrapBackend().available(),
    reason="the Node PTC runtime needs node and bubblewrap",
)


@requires_node
async def test_program_calls_a_host_tool_and_returns_a_value(tmp_path) -> None:
    """A program receives the tool result and its return value reaches the host."""
    runtime = NodePtcRuntime(get_settings())
    calls: list[dict[str, int]] = []

    async def add(arguments: dict[str, int]) -> int:
        calls.append(arguments)
        return arguments["a"] + arguments["b"]

    spec = runtime.resolve(
        """
const sum = await tool('add', { a: 2, b: 3 });
console.log('sum is', sum);
return { sum };
""",
        {"add": add},
        str(tmp_path),
        30_000,
        None,
    )

    outcome = await runtime.run(spec)

    assert outcome.status == "ok", outcome.error
    assert outcome.value == {"sum": 5}
    assert "sum is 5" in outcome.output
    assert calls == [{"a": 2, "b": 3}]
    assert outcome.sandboxed is True
    assert outcome.sandbox_backend == "bwrap"
    assert [call.status for call in outcome.tool_calls] == ["ok"]


@requires_node
async def test_unknown_tool_name_is_refused_to_the_program(tmp_path) -> None:
    """A name outside the binding map fails the program's call, not the host."""
    runtime = NodePtcRuntime(get_settings())
    spec = runtime.resolve(
        """
try {
  await tool('shell', { cmd: 'id' });
  return 'unexpected';
} catch (error) {
  return 'refused: ' + error.message;
}
""",
        {},
        str(tmp_path),
        30_000,
        None,
    )

    outcome = await runtime.run(spec)

    assert outcome.status == "ok", outcome.error
    assert outcome.value.startswith("refused:")
    assert outcome.tool_calls[0].status == "refused"


@requires_node
async def test_tool_call_budget_is_enforced(tmp_path) -> None:
    """The program stops calling host tools at the configured budget."""
    runtime = NodePtcRuntime(get_settings())

    async def echo(arguments: dict[str, int]) -> int:
        return arguments["value"]

    spec = runtime.resolve(
        """
const values = [];
for (let index = 0; index < 3; index += 1) {
  try {
    values.push(await tool('echo', { value: index }));
  } catch (error) {
    values.push('limited');
  }
}
return values;
""",
        {"echo": echo},
        str(tmp_path),
        30_000,
        None,
        max_tool_calls=2,
    )

    outcome = await runtime.run(spec)

    assert outcome.status == "ok", outcome.error
    assert outcome.value == [0, 1, "limited"]
    assert [call.status for call in outcome.tool_calls] == ["ok", "ok", "refused"]


@requires_node
async def test_endless_program_is_killed_at_the_budget(tmp_path) -> None:
    """A program that never settles times out and is reaped."""
    runtime = NodePtcRuntime(get_settings())
    spec = runtime.resolve(
        """
await new Promise(() => {});
return 'never';
""",
        {},
        str(tmp_path),
        1_000,
        None,
    )

    outcome = await runtime.run(spec)

    assert outcome.status == "timeout"
    assert "1000ms" in (outcome.error or "")
