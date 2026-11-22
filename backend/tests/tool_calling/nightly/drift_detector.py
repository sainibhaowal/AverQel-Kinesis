"""Drift detector - compares nightly results with baseline to detect regressions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DriftReport:
    """Report of detected drift between baseline and current."""

    has_drift: bool = False
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics_comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_drift": self.has_drift,
            "issues": self.issues,
            "warnings": self.warnings,
            "metrics_comparison": self.metrics_comparison,
        }


class DriftDetector:
    """Detects drift in tool-calling behavior between runs."""

    def __init__(self, fixture_dir: str = "fixtures"):
        self.fixture_dir = fixture_dir

    def detect_drift(
        self,
        baseline_results: dict[str, Any],
        fresh_results: list[dict[str, Any]],
    ) -> DriftReport:
        """Compare baseline replay results with fresh scenario results."""
        report = DriftReport()

        # 1. Check baseline success rate
        baseline_total = baseline_results.get("total", 0)
        baseline_passed = baseline_results.get("passed", 0)
        baseline_failed = baseline_results.get("failed", 0)

        if baseline_total > 0:
            baseline_success_rate = baseline_passed / baseline_total
            if baseline_success_rate < 1.0:
                report.has_drift = True
                report.issues.append(
                    f"Baseline fixture replay success rate dropped to {baseline_success_rate:.1%} "
                    f"({baseline_passed}/{baseline_total} passed)"
                )

        # 2. Check fresh scenario success rate
        if fresh_results:
            fresh_total = len(fresh_results)
            fresh_passed = sum(1 for r in fresh_results if r.get("success", False))
            fresh_success_rate = fresh_passed / fresh_total

            if fresh_success_rate < 0.8:  # Allow 20% failure for fresh scenarios
                report.has_drift = True
                report.issues.append(
                    f"Fresh scenario success rate only {fresh_success_rate:.1%} "
                    f"({fresh_passed}/{fresh_total} passed)"
                )
            elif fresh_success_rate < 0.95:
                report.warnings.append(
                    f"Fresh scenario success rate {fresh_success_rate:.1%} below 95%"
                )

        # 3. Compare tool usage patterns
        baseline_tool_usage = self._extract_tool_usage(baseline_results)
        fresh_tool_usage = self._extract_tool_usage_from_fresh(fresh_results)

        for tool, baseline_count in baseline_tool_usage.items():
            fresh_count = fresh_tool_usage.get(tool, 0)
            if baseline_count > 0 and fresh_count == 0:
                report.warnings.append(
                    f"Tool '{tool}' used in baseline ({baseline_count}x) but not in fresh scenarios"
                )

        # 4. Check for new errors in fresh scenarios
        fresh_errors = []
        for r in fresh_results:
            fresh_errors.extend(r.get("errors", []))

        if fresh_errors:
            error_types = {}
            for e in fresh_errors:
                etype = e.get("type", "unknown")
                error_types[etype] = error_types.get(etype, 0) + 1

            for etype, count in error_types.items():
                report.warnings.append(f"Fresh scenarios had {count} '{etype}' errors")

        # 5. Performance drift (tool call latency)
        baseline_avg_duration = self._calc_avg_duration(baseline_results)
        fresh_avg_duration = self._calc_avg_duration_fresh(fresh_results)

        if baseline_avg_duration > 0 and fresh_avg_duration > 0:
            ratio = fresh_avg_duration / baseline_avg_duration
            if ratio > 2.0:
                report.warnings.append(
                    f"Fresh scenarios {ratio:.1f}x slower than baseline "
                    f"({fresh_avg_duration:.0f}ms vs {baseline_avg_duration:.0f}ms avg)"
                )

        report.metrics_comparison = {
            "baseline": {
                "total": baseline_total,
                "passed": baseline_passed,
                "failed": baseline_failed,
                "success_rate": baseline_success_rate if baseline_total > 0 else 0,
                "avg_duration_ms": baseline_avg_duration,
            },
            "fresh": {
                "total": len(fresh_results),
                "passed": fresh_passed if fresh_results else 0,
                "success_rate": fresh_success_rate if fresh_results else 0,
                "avg_duration_ms": fresh_avg_duration,
            },
            "tool_usage_baseline": baseline_tool_usage,
            "tool_usage_fresh": fresh_tool_usage,
        }

        return report

    def _extract_tool_usage(self, results: dict[str, Any]) -> dict[str, int]:
        """Extract tool usage counts from baseline replay results."""
        usage = {}
        for detail in results.get("details", []):
            # Would need tool_calls in details
            pass
        return usage

    def _extract_tool_usage_from_fresh(self, fresh_results: list[dict[str, Any]]) -> dict[str, int]:
        """Extract tool usage from fresh scenario results."""
        usage = {}
        for r in fresh_results:
            # Tool calls not directly stored, would need trace
            pass
        return usage

    def _calc_avg_duration(self, results: dict[str, Any]) -> float:
        """Calculate average duration from baseline results."""
        durations = [d.get("duration_ms", 0) for d in results.get("details", [])]
        return sum(durations) / len(durations) if durations else 0

    def _calc_avg_duration_fresh(self, fresh_results: list[dict[str, Any]]) -> float:
        """Calculate average duration from fresh results."""
        # Would need timing info
        return 0

    def compare_fixture_versions(
        self,
        old_fixtures: dict[str, list],
        new_fixtures: dict[str, list],
    ) -> DriftReport:
        """Compare two versions of fixtures directly."""
        report = DriftReport()

        all_tools_old = set()
        all_tools_new = set()

        for category, fixtures in old_fixtures.items():
            for f in fixtures:
                for tc in f.get("tool_calls", []):
                    all_tools_old.add(tc.get("tool_name", ""))

        for category, fixtures in new_fixtures.items():
            for f in fixtures:
                for tc in f.get("tool_calls", []):
                    all_tools_new.add(tc.get("tool_name", ""))

        # Tools that disappeared
        missing = all_tools_old - all_tools_new
        if missing:
            report.has_drift = True
            report.issues.append(f"Tools no longer called: {missing}")

        # New tools appearing
        new_tools = all_tools_new - all_tools_old
        if new_tools:
            report.warnings.append(f"New tools being called: {new_tools}")

        return report
