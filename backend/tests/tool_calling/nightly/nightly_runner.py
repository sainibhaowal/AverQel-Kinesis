"""Nightly runner - executes fresh scenarios against real LM Studio model."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..disposable_backend import BackendTestClient, DisposableBackend
from ..lm_studio_client import (
    ToolCallingOrchestrator,
    create_lm_studio_client,
)
from ..recorder.fixture_serializer import FixtureSerializer
from .drift_detector import DriftDetector, DriftReport
from .fresh_scenario_generator import FreshScenarioGenerator


@dataclass
class NightlyConfig:
    """Configuration for nightly run."""

    lm_studio_url: str = "http://localhost:1234/v1"
    model: str = "spark-x2.6-1.7b"
    fixture_dir: str = "fixtures"
    report_dir: str = "nightly_reports"
    max_turns: int = 20
    max_tool_calls_per_turn: int = 10
    categories: list[str] = field(default_factory=lambda: ["core", "advanced", "chaining", "error"])
    priorities: list[int] = field(default_factory=lambda: [0, 1, 2])
    include_fresh_scenarios: bool = True
    fresh_scenario_count: int = 10
    compare_with_baseline: bool = True


@dataclass
class NightlyRunResult:
    """Result of a nightly run."""

    timestamp: str
    config: NightlyConfig
    baseline_replay: dict[str, Any]
    fresh_scenarios: list[dict[str, Any]]
    drift_report: DriftReport | None = None
    total_duration_seconds: float = 0
    success: bool = True
    error: str | None = None


class NightlyRunner:
    """Runs nightly tool-calling verification with real LM Studio model."""

    def __init__(self, config: NightlyConfig):
        self.config = config
        self.report_dir = Path(config.report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.fixture_serializer = FixtureSerializer(config.fixture_dir)
        self.drift_detector = DriftDetector(config.fixture_dir)

    async def run(self) -> NightlyRunResult:
        """Execute the full nightly run."""
        start_time = time.time()
        timestamp = datetime.utcnow().isoformat() + "Z"

        print(f"Starting nightly run at {timestamp}")
        print(f"Model: {self.config.model}")
        print(f"Categories: {self.config.categories}")

        try:
            # 1. Replay baseline fixtures to verify they still work
            print("\n=== Phase 1: Baseline Fixture Replay ===")
            baseline_results = await self._replay_baseline()

            # 2. Generate and run fresh scenarios
            fresh_results = []
            if self.config.include_fresh_scenarios:
                print("\n=== Phase 2: Fresh Scenario Generation & Execution ===")
                fresh_results = await self._run_fresh_scenarios()

            # 3. Detect drift
            drift_report = None
            if self.config.compare_with_baseline:
                print("\n=== Phase 3: Drift Detection ===")
                drift_report = self.drift_detector.detect_drift(
                    baseline_results,
                    fresh_results,
                )
                self._print_drift_summary(drift_report)

            # 4. Save report
            result = NightlyRunResult(
                timestamp=timestamp,
                config=self.config,
                baseline_replay=baseline_results,
                fresh_scenarios=fresh_results,
                drift_report=drift_report,
                total_duration_seconds=time.time() - start_time,
                success=True,
            )

            self._save_report(result)

            print("\n=== Nightly Run Complete ===")
            print(f"Duration: {result.total_duration_seconds:.1f}s")
            print(f"Baseline fixtures: {baseline_results.get('total', 0)}")
            print(f"Fresh scenarios: {len(fresh_results)}")
            if drift_report:
                print(f"Drift detected: {drift_report.has_drift}")

            return result

        except Exception as e:
            print(f"Nightly run failed: {e}")
            return NightlyRunResult(
                timestamp=timestamp,
                config=self.config,
                baseline_replay={},
                fresh_scenarios=[],
                total_duration_seconds=time.time() - start_time,
                success=False,
                error=str(e),
            )

    async def _replay_baseline(self) -> dict[str, Any]:
        """Replay all baseline fixtures."""
        from ..replay.parallel_replay import parallel_replay_fixtures

        summary = await parallel_replay_fixtures(
            fixture_dir=self.config.fixture_dir,
            num_workers=2,  # Sequential-ish for stability
            categories=self.config.categories,
        )

        return {
            "total": summary.total_fixtures,
            "passed": summary.successful,
            "failed": summary.failed,
            "assertions_passed": summary.passed_assertions,
            "assertions_failed": summary.failed_assertions,
            "details": summary.details,
        }

    async def _run_fresh_scenarios(self) -> list[dict[str, Any]]:
        """Generate and execute fresh scenarios."""
        generator = FreshScenarioGenerator(
            lm_studio_url=self.config.lm_studio_url,
            model=self.config.model,
        )

        # Generate fresh scenarios
        fresh_scenarios = await generator.generate(
            count=self.config.fresh_scenario_count,
            categories=self.config.categories,
        )

        print(f"Generated {len(fresh_scenarios)} fresh scenarios")

        # Execute each fresh scenario
        results = []
        async with create_lm_studio_client(
            base_url=self.config.lm_studio_url,
            model=self.config.model,
        ) as lm_studio:
            for scenario in fresh_scenarios:
                print(f"  Running fresh scenario: {scenario.id}")

                backend = DisposableBackend()
                try:
                    await backend.initialize()
                    backend_client = BackendTestClient(backend)

                    orchestrator = ToolCallingOrchestrator(
                        lm_studio=lm_studio,
                        backend_client=backend_client,
                        tools=backend_client.get_available_tools(),
                        max_turns=self.config.max_turns,
                        max_tool_calls_per_turn=self.config.max_tool_calls_per_turn,
                    )

                    trace = await orchestrator.run_scenario(
                        scenario_id=scenario.id,
                        prompt=scenario.prompt,
                    )

                    # Save as new fixture (for future baseline)
                    self.fixture_serializer.save_trace(trace, scenario.category)

                    results.append(
                        {
                            "scenario_id": scenario.id,
                            "category": scenario.category,
                            "prompt": scenario.prompt,
                            "success": len(trace.errors) == 0,
                            "tool_calls": len(trace.tool_calls),
                            "turns": len(trace.turns),
                            "errors": trace.errors,
                        }
                    )

                finally:
                    await backend.close()

        return results

    def _print_drift_summary(self, drift_report: DriftReport) -> None:
        """Print drift detection summary."""
        if drift_report.has_drift:
            print("⚠️  DRIFT DETECTED:")
            for issue in drift_report.issues:
                print(f"  - {issue}")
        else:
            print("✓ No drift detected")

    def _save_report(self, result: NightlyRunResult) -> None:
        """Save nightly run report."""
        report_file = self.report_dir / f"nightly_{result.timestamp.replace(':', '-')}.json"

        # Convert to serializable dict
        report_data = {
            "timestamp": result.timestamp,
            "config": {
                "model": result.config.model,
                "lm_studio_url": result.config.lm_studio_url,
                "categories": result.config.categories,
                "priorities": result.config.priorities,
            },
            "baseline_replay": result.baseline_replay,
            "fresh_scenarios": result.fresh_scenarios,
            "drift_report": result.drift_report.to_dict() if result.drift_report else None,
            "total_duration_seconds": result.total_duration_seconds,
            "success": result.success,
            "error": result.error,
        }

        with open(report_file, "w") as f:
            json.dump(report_data, f, indent=2, default=str)

        print(f"Report saved to {report_file}")

        # Also save latest symlink
        latest_link = self.report_dir / "latest.json"
        try:
            latest_link.unlink()
        except FileNotFoundError:
            pass
        latest_link.symlink_to(report_file.name)


async def run_nightly(
    config: NightlyConfig | None = None,
) -> NightlyRunResult:
    """Convenience function to run nightly verification."""
    if config is None:
        config = NightlyConfig()

    runner = NightlyRunner(config)
    return await runner.run()
