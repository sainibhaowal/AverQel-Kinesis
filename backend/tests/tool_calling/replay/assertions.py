"""Comprehensive assertions for tool-calling replay verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..scenario_library import Scenario
from .replay_engine import ReplayResult


@dataclass
class AssertionResult:
    """Result of a single assertion check."""

    name: str
    passed: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ReplayAssertions:
    """Comprehensive assertion suite for replay verification."""

    @staticmethod
    def assert_no_errors(result: ReplayResult) -> AssertionResult:
        """Assert no tool execution errors occurred."""
        error_count = len(result.errors)
        return AssertionResult(
            name="no_errors",
            passed=error_count == 0,
            message=(
                f"Found {error_count} tool execution errors" if error_count > 0 else "No errors"
            ),
            details={"error_count": error_count, "errors": result.errors},
        )

    @staticmethod
    def assert_min_tool_calls(result: ReplayResult, min_calls: int) -> AssertionResult:
        """Assert minimum number of tool calls were made."""
        actual = len(result.tool_results)
        return AssertionResult(
            name="min_tool_calls",
            passed=actual >= min_calls,
            message=f"Expected at least {min_calls} tool calls, got {actual}",
            details={"expected_min": min_calls, "actual": actual},
        )

    @staticmethod
    def assert_max_tool_calls(result: ReplayResult, max_calls: int) -> AssertionResult:
        """Assert maximum number of tool calls not exceeded."""
        actual = len(result.tool_results)
        return AssertionResult(
            name="max_tool_calls",
            passed=actual <= max_calls,
            message=f"Expected at most {max_calls} tool calls, got {actual}",
            details={"expected_max": max_calls, "actual": actual},
        )

    @staticmethod
    def assert_expected_tools_used(
        result: ReplayResult, expected_tools: list[str]
    ) -> AssertionResult:
        """Assert all expected tools were called."""
        tools_used = {tr["tool_name"] for tr in result.tool_results}
        expected = set(expected_tools)
        missing = expected - tools_used

        return AssertionResult(
            name="expected_tools_used",
            passed=len(missing) == 0,
            message=f"Missing expected tools: {missing}" if missing else "All expected tools used",
            details={
                "expected": list(expected),
                "used": list(tools_used),
                "missing": list(missing),
            },
        )

    @staticmethod
    def assert_no_unexpected_tools(
        result: ReplayResult, allowed_tools: list[str] | None = None
    ) -> AssertionResult:
        """Assert no unexpected tools were called."""
        if not allowed_tools:
            return AssertionResult(
                name="no_unexpected_tools", passed=True, message="No tool restrictions"
            )

        tools_used = {tr["tool_name"] for tr in result.tool_results}
        allowed = set(allowed_tools)
        unexpected = tools_used - allowed

        return AssertionResult(
            name="no_unexpected_tools",
            passed=len(unexpected) == 0,
            message=(
                f"Unexpected tools called: {unexpected}" if unexpected else "No unexpected tools"
            ),
            details={
                "allowed": list(allowed),
                "used": list(tools_used),
                "unexpected": list(unexpected),
            },
        )

    @staticmethod
    def assert_tool_call_order(result: ReplayResult, expected_order: list[str]) -> AssertionResult:
        """Assert tools were called in expected order."""
        actual_order = [tr["tool_name"] for tr in result.tool_results]

        # Check if expected_order is a subsequence of actual_order
        expected_idx = 0
        for tool in actual_order:
            if expected_idx < len(expected_order) and tool == expected_order[expected_idx]:
                expected_idx += 1

        passed = expected_idx == len(expected_order)

        return AssertionResult(
            name="tool_call_order",
            passed=passed,
            message=(
                f"Expected order {expected_order}, got {actual_order}"
                if not passed
                else "Order matches"
            ),
            details={"expected_order": expected_order, "actual_order": actual_order},
        )

    @staticmethod
    def assert_tool_arguments_valid(result: ReplayResult) -> AssertionResult:
        """Assert all tool calls had valid arguments (no validation errors)."""
        validation_errors = [
            tr
            for tr in result.tool_results
            if tr.get("result", {}).get("status") == "validation_error"
        ]

        return AssertionResult(
            name="tool_arguments_valid",
            passed=len(validation_errors) == 0,
            message=(
                f"{len(validation_errors)} validation errors"
                if validation_errors
                else "All arguments valid"
            ),
            details={"validation_errors": validation_errors},
        )

    @staticmethod
    def assert_no_tool_timeouts(
        result: ReplayResult, max_duration_ms: float = 30000
    ) -> AssertionResult:
        """Assert no tool calls timed out."""
        # Check if any tool took too long
        [
            tr
            for tr in result.tool_results
            # We'd need timing info in tool_results
        ]

        # For now, just check for timeout errors
        timeout_errors = [
            tr
            for tr in result.tool_results
            if "timeout" in str(tr.get("result", {}).get("error", "")).lower()
        ]

        return AssertionResult(
            name="no_timeouts",
            passed=len(timeout_errors) == 0,
            message=f"{len(timeout_errors)} timeout errors" if timeout_errors else "No timeouts",
            details={"timeout_errors": timeout_errors},
        )

    @staticmethod
    def assert_result_structure_valid(result: ReplayResult) -> AssertionResult:
        """Assert all tool results have valid structure."""
        invalid = []
        for tr in result.tool_results:
            if "tool_name" not in tr:
                invalid.append("missing tool_name")
            if "tool_call_id" not in tr:
                invalid.append("missing tool_call_id")
            if "result" not in tr:
                invalid.append("missing result")

        return AssertionResult(
            name="result_structure_valid",
            passed=len(invalid) == 0,
            message=f"Structural issues: {invalid}" if invalid else "All results well-formed",
            details={"invalid_count": len(invalid)},
        )

    @staticmethod
    def assert_all_tools_completed(result: ReplayResult) -> AssertionResult:
        """Assert all tool calls completed (no hanging)."""
        # In replay, if we got results for all calls, they completed
        expected = len(result.fixture.trace.tool_calls)
        actual = len(result.tool_results)

        return AssertionResult(
            name="all_tools_completed",
            passed=actual >= expected,
            message=f"Expected {expected} tool results, got {actual}",
            details={"expected": expected, "actual": actual},
        )

    @staticmethod
    def assert_consistent_with_fixture(result: ReplayResult) -> AssertionResult:
        """Assert replay results are consistent with original fixture."""
        mismatches = result.mismatch_details

        return AssertionResult(
            name="consistent_with_fixture",
            passed=len(mismatches) == 0,
            message=(
                f"{len(mismatches)} mismatches with recorded fixture"
                if mismatches
                else "Consistent with fixture"
            ),
            details={"mismatches": mismatches},
        )

    @staticmethod
    def assert_scenario_assertions(result: ReplayResult) -> AssertionResult:
        """Assert all scenario-specific assertions passed."""
        failed = [name for name, passed in result.assertions_passed.items() if not passed]

        return AssertionResult(
            name="scenario_assertions",
            passed=len(failed) == 0,
            message=f"Failed assertions: {failed}" if failed else "All scenario assertions passed",
            details={"passed": result.assertions_passed, "failed": failed},
        )

    @staticmethod
    def run_all_assertions(
        result: ReplayResult,
        scenario: Scenario,
    ) -> list[AssertionResult]:
        """Run all standard assertions for a replay result."""
        assertions = []

        # Standard assertions
        assertions.append(ReplayAssertions.assert_no_errors(result))
        assertions.append(ReplayAssertions.assert_result_structure_valid(result))
        assertions.append(ReplayAssertions.assert_all_tools_completed(result))
        assertions.append(ReplayAssertions.assert_consistent_with_fixture(result))
        assertions.append(ReplayAssertions.assert_scenario_assertions(result))

        # Scenario-specific assertions
        if scenario.min_tool_calls > 1:
            assertions.append(
                ReplayAssertions.assert_min_tool_calls(result, scenario.min_tool_calls)
            )
        if scenario.max_tool_calls:
            assertions.append(
                ReplayAssertions.assert_max_tool_calls(result, scenario.max_tool_calls)
            )
        if scenario.expected_tools:
            assertions.append(
                ReplayAssertions.assert_expected_tools_used(result, scenario.expected_tools)
            )

        return assertions

    @staticmethod
    def summarize_assertions(assertions: list[AssertionResult]) -> dict[str, Any]:
        """Summarize assertion results."""
        passed = sum(1 for a in assertions if a.passed)
        failed = len(assertions) - passed

        return {
            "total": len(assertions),
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / len(assertions) if assertions else 1.0,
            "details": [
                {
                    "name": a.name,
                    "passed": a.passed,
                    "message": a.message,
                }
                for a in assertions
            ],
        }
