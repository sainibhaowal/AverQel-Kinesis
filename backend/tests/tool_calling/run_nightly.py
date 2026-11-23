#!/usr/bin/env python3
"""
CLI entry point for nightly tool-calling verification (Approach #1).

Usage:
    python -m backend.tests.tool_calling.run_nightly --help
    python -m backend.tests.tool_calling.run_nightly --fresh-count 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add backend to path
BACKEND_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from tests.tool_calling.nightly.alerter import Alerter, create_alert_config_from_env
from tests.tool_calling.nightly.nightly_runner import NightlyConfig, NightlyRunner


async def main():
    parser = argparse.ArgumentParser(
        description="Nightly tool-calling verification with real LM Studio"
    )

    parser.add_argument(
        "--model",
        "-m",
        default=os.getenv("LM_STUDIO_MODEL", "spark-x2.6-1.7b"),
        help="LM Studio model to use",
    )
    parser.add_argument(
        "--url",
        "-u",
        default=os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"),
        help="LM Studio base URL",
    )
    parser.add_argument(
        "--fixture-dir", "-f", default="fixtures", help="Fixture directory for baseline comparison"
    )
    parser.add_argument(
        "--report-dir", "-r", default="nightly_reports", help="Directory for nightly reports"
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=["core", "advanced", "chaining", "error", "specialized"],
        default=["core", "advanced", "chaining", "error"],
        help="Categories to test",
    )
    parser.add_argument(
        "--priorities",
        nargs="+",
        type=int,
        choices=[0, 1, 2],
        default=[0, 1, 2],
        help="Priorities to test",
    )
    parser.add_argument(
        "--fresh-count", type=int, default=10, help="Number of fresh scenarios to generate"
    )
    parser.add_argument("--no-fresh", action="store_true", help="Skip fresh scenario generation")
    parser.add_argument("--no-drift", action="store_true", help="Skip drift detection")
    parser.add_argument("--alert", action="store_true", help="Send alerts on failure/drift")

    args = parser.parse_args()

    config = NightlyConfig(
        lm_studio_url=args.url,
        model=args.model,
        fixture_dir=args.fixture_dir,
        report_dir=args.report_dir,
        categories=args.categories,
        priorities=args.priorities,
        include_fresh_scenarios=not args.no_fresh,
        fresh_scenario_count=args.fresh_count,
        compare_with_baseline=not args.no_drift,
    )

    print("Starting nightly run:")
    print(f"  Model: {config.model}")
    print(f"  LM Studio: {config.lm_studio_url}")
    print(f"  Categories: {config.categories}")
    print(f"  Priorities: {config.priorities}")
    print(
        f"  Fresh scenarios: {config.fresh_scenario_count if config.include_fresh_scenarios else 'disabled'}"
    )
    print(f"  Drift detection: {'enabled' if config.compare_with_baseline else 'disabled'}")
    print()

    try:
        runner = NightlyRunner(config)
        result = await runner.run()

        # Send alerts if requested
        if args.alert:
            alert_config = create_alert_config_from_env()
            alerter = Alerter(alert_config)
            await alerter.send_alert(result, result.drift_report)

        # Print final status
        print(f"\n{'='*50}")
        print(f"NIGHTLY RUN {'SUCCESS' if result.success else 'FAILED'}")
        print(f"{'='*50}")
        print(f"Duration: {result.total_duration_seconds:.1f}s")
        print(
            f"Baseline: {result.baseline_replay.get('passed', 0)}/{result.baseline_replay.get('total', 0)} passed"
        )
        print(
            f"Fresh: {len([r for r in result.fresh_scenarios if r.get('success', False)])}/{len(result.fresh_scenarios)} passed"
        )

        if result.drift_report:
            print(f"Drift: {'DETECTED' if result.drift_report.has_drift else 'NONE'}")
            if result.drift_report.has_drift:
                for issue in result.drift_report.issues:
                    print(f"  ISSUE: {issue}")

        if not result.success:
            print(f"Error: {result.error}")
            return 1

        if result.drift_report and result.drift_report.has_drift:
            print("⚠️  Drift detected - review required")
            return 2  # Special exit code for drift

        return 0

    except Exception as e:
        print(f"\n✗ Nightly run failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
