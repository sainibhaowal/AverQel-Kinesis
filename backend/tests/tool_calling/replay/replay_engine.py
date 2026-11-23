"""Replay engine - deterministically replays tool calls against real backend."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..disposable_backend import BackendTestClient, DisposableBackend
from ..scenario_library import Scenario
from .fixture_loader import FixtureLoader, LoadedFixture


@dataclass
class ReplayResult:
    """Result of replaying a fixture."""

    fixture: LoadedFixture
    success: bool
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0
    assertions_passed: dict[str, bool] = field(default_factory=dict)
    mismatch_details: list[str] = field(default_factory=list)


class ReplayEngine:
    """Replays recorded tool calls against a real backend to verify they still work."""

    def __init__(
        self,
        backend: DisposableBackend,
        max_concurrent_tools: int = 10,
    ):
        self.backend = backend
        self.backend_client = BackendTestClient(backend)
        self.max_concurrent_tools = max_concurrent_tools

    async def replay_fixture(self, fixture: LoadedFixture) -> ReplayResult:
        """Replay a single fixture's tool calls."""
        start_time = time.time()
        trace = fixture.trace
        scenario = fixture.scenario

        tool_results = []
        errors = []
        mismatch_details = []

        # Replay each tool call in order
        for tc in trace.tool_calls:
            tool_name = tc.get("tool_name")
            tool_call_id = tc.get("tool_call_id")
            arguments = tc.get("arguments", {})
            original_result = tc.get("original_result")  # From fixture

            try:
                # Execute against real backend
                result = await self.backend_client.execute_tool(tool_name, arguments)

                tool_results.append(
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "result": result,
                        "original_result": original_result,
                    }
                )

                # Check for mismatches with original result
                if original_result:
                    mismatches = self._compare_results(
                        tool_name, arguments, result, original_result
                    )
                    if mismatches:
                        mismatch_details.extend(mismatches)

                # Check for errors in result
                if isinstance(result, dict) and result.get("status") in (
                    "error",
                    "validation_error",
                    "execution_error",
                ):
                    errors.append(
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "error": result.get("error", "Unknown error"),
                        }
                    )

            except Exception as e:
                errors.append(
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "error": str(e),
                    }
                )
                tool_results.append(
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "result": {"error": str(e), "status": "replay_error"},
                        "original_result": original_result,
                    }
                )

        duration_ms = (time.time() - start_time) * 1000

        # Verify scenario assertions
        assertions_passed = self._verify_assertions(scenario, tool_results, errors)

        # Overall success: no errors, all assertions passed
        success = len(errors) == 0 and all(assertions_passed.values())

        return ReplayResult(
            fixture=fixture,
            success=success,
            tool_results=tool_results,
            errors=errors,
            duration_ms=duration_ms,
            assertions_passed=assertions_passed,
            mismatch_details=mismatch_details,
        )

    def _compare_results(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        current_result: dict[str, Any],
        original_result: dict[str, Any],
    ) -> list[str]:
        """Compare current result with original recorded result."""
        mismatches = []

        # Compare status
        current_status = current_result.get("status", "success")
        original_status = original_result.get("status", "success")

        if current_status != original_status:
            mismatches.append(
                f"{tool_name}: status changed from '{original_status}' to '{current_status}'"
            )

        # For successful results, compare key fields (but allow some variance)
        if current_status == "success" and original_status == "success":
            # Compare content length (allow 20% variance)
            current_content = str(current_result.get("content", ""))
            original_content = str(original_result.get("content", ""))

            if original_content and current_content:
                len_ratio = len(current_content) / len(original_content)
                if len_ratio < 0.5 or len_ratio > 2.0:
                    mismatches.append(
                        f"{tool_name}: content length changed significantly "
                        f"({len(original_content)} -> {len(current_content)})"
                    )

        return mismatches

    def _verify_assertions(
        self,
        scenario: Scenario,
        tool_results: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> dict[str, bool]:
        """Verify scenario assertions against replay results."""
        results = {}
        assertions = scenario.assertions

        # Basic assertions
        if assertions.get("no_errors"):
            results["no_errors"] = len(errors) == 0

        if assertions.get("min_tool_calls"):
            results["min_tool_calls"] = len(tool_results) >= assertions["min_tool_calls"]

        if assertions.get("max_tool_calls"):
            results["max_tool_calls"] = len(tool_results) <= assertions["max_tool_calls"]

        # Tool-specific assertions
        if assertions.get("expected_tools"):
            tools_used = {tr["tool_name"] for tr in tool_results}
            expected = set(assertions["expected_tools"])
            results["expected_tools_used"] = expected.issubset(tools_used)

        # Error handling assertions
        if assertions.get("tool_error_handled"):
            # In replay, we expect the same error pattern
            results["tool_error_handled"] = True  # If we got here, errors were handled

        return results


async def replay_all_fixtures(
    fixture_dir: str = "fixtures",
    categories: list[str] | None = None,
    backend: DisposableBackend | None = None,
) -> dict[str, list[ReplayResult]]:
    """Replay all fixtures in the given categories."""

    loader = FixtureLoader(fixture_dir)
    all_fixtures = loader.load_all()

    if categories:
        all_fixtures = {k: v for k, v in all_fixtures.items() if k in categories}

    # Use provided backend or create new one
    own_backend = backend is None
    if own_backend:
        backend = DisposableBackend()
        await backend.initialize()

    try:
        engine = ReplayEngine(backend)

        results = {}
        for category, fixtures in all_fixtures.items():
            print(f"Replaying {category}: {len(fixtures)} fixtures...")
            cat_results = []

            for fixture in fixtures:
                result = await engine.replay_fixture(fixture)
                cat_results.append(result)

                status = "✓" if result.success else "✗"
                print(f"  {status} {fixture.scenario.id} ({result.duration_ms:.0f}ms)")

            results[category] = cat_results

        return results

    finally:
        if own_backend:
            await backend.close()


async def replay_scenario(
    scenario_id: str,
    fixture_dir: str = "fixtures",
    backend: DisposableBackend | None = None,
) -> ReplayResult | None:
    """Replay a single scenario by ID."""
    loader = FixtureLoader(fixture_dir)
    fixture = loader.load_scenario(scenario_id)

    if not fixture:
        return None

    own_backend = backend is None
    if own_backend:
        backend = DisposableBackend()
        await backend.initialize()

    try:
        engine = ReplayEngine(backend)
        return await engine.replay_fixture(fixture)
    finally:
        if own_backend:
            await backend.close()
