#!/usr/bin/env python3
"""
CLI entry point for recording tool-calling fixtures (Approach #2).

Usage:
    python -m backend.tests.tool_calling.run_recorder --help
    python -m backend.tests.tool_calling.run_recorder --category core --workers 4
    python -m backend.tests.tool_calling.run_recorder --all --workers 4
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

from tests.tool_calling.recorder.parallel_recorder import parallel_record_scenarios
from tests.tool_calling.scenario_library import load_scenarios


def find_scenario_files() -> list[str]:
    """Find all scenario YAML files."""
    scenario_dir = Path(__file__).parent / "scenario_library"
    return sorted(str(f) for f in scenario_dir.glob("*.yaml"))


async def main():
    parser = argparse.ArgumentParser(description="Record tool-calling fixtures with LM Studio")

    parser.add_argument(
        "--category",
        "-c",
        choices=["core", "advanced", "chaining", "error", "specialized", "all"],
        default="core",
        help="Category of scenarios to record",
    )
    parser.add_argument(
        "--priority",
        "-p",
        type=int,
        choices=[0, 1, 2],
        help="Filter by priority (0=P0, 1=P1, 2=P2)",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=4, help="Number of parallel workers (default: 4)"
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
        "--fixture-dir", "-f", default="fixtures", help="Output directory for fixtures"
    )
    parser.add_argument(
        "--scenario-files", nargs="+", help="Specific scenario files to use (default: all)"
    )
    parser.add_argument(
        "--list-scenarios", action="store_true", help="List available scenarios and exit"
    )

    args = parser.parse_args()

    # Find scenario files
    if args.scenario_files:
        scenario_files = args.scenario_files
    else:
        scenario_files = find_scenario_files()

    if args.list_scenarios:
        print("Available scenario files:")
        for f in scenario_files:
            scenarios = load_scenarios(f)
            print(f"  {f}: {len(scenarios)} scenarios")
            for s in scenarios:
                print(f"    {s.id} (P{s.priority}, {s.category}) - {s.prompt[:80]}...")
        return 0

    # Determine categories
    if args.category == "all":
        categories = ["core", "advanced", "chaining", "error", "specialized"]
    else:
        categories = [args.category]

    priorities = [args.priority] if args.priority is not None else None

    print(f"Recording scenarios from: {scenario_files}")
    print(f"Categories: {categories}")
    print(f"Priorities: {priorities or 'all'}")
    print(f"Workers: {args.workers}")
    print(f"Model: {args.model}")
    print(f"LM Studio: {args.url}")
    print(f"Fixture dir: {args.fixture_dir}")
    print()

    # Run recording
    try:
        summary = await parallel_record_scenarios(
            scenario_files=scenario_files,
            num_workers=args.workers,
            lm_studio_url=args.url,
            model=args.model,
            fixture_dir=args.fixture_dir,
            categories=categories,
            priorities=priorities,
        )

        # Exit with error code if any failed
        if summary["failed"] > 0:
            print(f"\n⚠️  {summary['failed']} scenarios failed!")
            return 1

        print(f"\n✓ All {summary['successful']} scenarios recorded successfully!")
        return 0

    except Exception as e:
        print(f"\n✗ Recording failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
