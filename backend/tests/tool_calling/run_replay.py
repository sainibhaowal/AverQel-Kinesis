#!/usr/bin/env python3
"""
CLI entry point for replaying tool-calling fixtures (CI-safe).

Usage:
    python -m backend.tests.tool_calling.run_replay --help
    python -m backend.tests.tool_calling.run_replay --category core --workers 4
    python -m backend.tests.tool_calling.run_replay --all --workers 4
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Add backend to path
BACKEND_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from tests.tool_calling.replay.parallel_replay import parallel_replay_fixtures


async def main():
    parser = argparse.ArgumentParser(description="Replay tool-calling fixtures (CI-safe)")

    parser.add_argument(
        "--category",
        "-c",
        choices=["core", "advanced", "chaining", "error", "specialized", "all"],
        default="all",
        help="Category of fixtures to replay",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=4, help="Number of parallel workers (default: 4)"
    )
    parser.add_argument(
        "--fixture-dir", "-f", default="fixtures", help="Directory containing fixtures"
    )
    parser.add_argument(
        "--list-fixtures", action="store_true", help="List available fixtures and exit"
    )
    parser.add_argument(
        "--validate", action="store_true", help="Validate fixture integrity and exit"
    )
    parser.add_argument("--summary", action="store_true", help="Show fixture summary and exit")

    args = parser.parse_args()

    fixture_dir = Path(args.fixture_dir)
    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}")
        return 1

    if args.list_fixtures or args.summary or args.validate:
        from tests.tool_calling.recorder.fixture_serializer import FixtureSerializer

        serializer = FixtureSerializer(str(fixture_dir))

        if args.list_fixtures:
            all_fixtures = serializer.load_all()
            for category, traces in all_fixtures.items():
                print(f"{category}: {len(traces)} fixtures")
                for t in traces:
                    print(f"  {t.scenario_id}: {t.prompt[:80]}... ({len(t.tool_calls)} calls)")
            return 0

        if args.summary:
            summary = serializer.export_summary()
            print(f"Total scenarios: {summary['total_scenarios']}")
            print(f"Total tool calls: {summary['total_tool_calls']}")
            print(f"Total turns: {summary['total_turns']}")
            print("\nBy category:")
            for cat, stats in summary["categories"].items():
                print(
                    f"  {cat}: {stats['scenario_count']} scenarios, {stats['total_tool_calls']} calls"
                )
            print("\nTool coverage:")
            for tool, count in sorted(summary["tool_coverage"].items(), key=lambda x: -x[1]):
                print(f"  {tool}: {count}")
            return 0

        if args.validate:
            validation = serializer.validate_fixtures()
            if validation["valid"]:
                print("✓ All fixtures valid")
                print(f"  Categories: {validation['categories_checked']}")
                print(f"  Total traces: {validation['total_traces']}")
            else:
                print("✗ Fixture validation failed:")
                for issue in validation["issues"]:
                    print(f"  - {issue}")
            return 0 if validation["valid"] else 1

    # Determine categories
    if args.category == "all":
        categories = None  # All categories
    else:
        categories = [args.category]

    print(f"Replaying fixtures from: {fixture_dir}")
    print(f"Categories: {categories or 'all'}")
    print(f"Workers: {args.workers}")
    print()

    # Create a shared backend for replay (or one per worker in parallel_replay)
    # The parallel_replay creates its own backends

    try:
        summary = await parallel_replay_fixtures(
            fixture_dir=str(fixture_dir),
            num_workers=args.workers,
            categories=categories,
        )

        # Print summary
        print(f"\n{'='*50}")
        print("REPLAY SUMMARY")
        print(f"{'='*50}")
        print(f"Total fixtures: {summary.total_fixtures}")
        print(f"Passed: {summary.successful}")
        print(f"Failed: {summary.failed}")
        print(f"Assertions: {summary.passed_assertions}/{summary.total_assertions} passed")
        print(f"Wall time: {summary.wall_time_seconds:.1f}s")
        print(f"CPU time: {summary.total_duration_ms/1000:.1f}s")

        if summary.by_category:
            print("\nBy category:")
            for cat, stats in summary.by_category.items():
                print(f"  {cat}: {stats['success']}/{stats['total']} passed")

        if summary.failed > 0:
            print(f"\n⚠️  {summary.failed} fixtures failed!")
            for d in summary.details:
                if not d["success"]:
                    print(f"  - {d['scenario_id']} ({d['category']})")
                    if d["errors"]:
                        for e in d["errors"]:
                            print(f"      Error: {e.get('error', 'unknown')}")
                    if d["error"]:
                        print(f"      Exception: {d['error']}")
            return 1

        print("\n✓ All fixtures replayed successfully!")
        return 0

    except Exception as e:
        print(f"\n✗ Replay failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
