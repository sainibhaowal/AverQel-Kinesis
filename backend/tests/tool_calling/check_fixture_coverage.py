"""Validate recorded fixture coverage against the native tool inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.tool_calling.recorder.fixture_serializer import FixtureSerializer


def verify_fixture_coverage(fixture_dir: Path, inventory_path: Path) -> int:
    with inventory_path.open(encoding="utf-8") as inventory_file:
        inventory = json.load(inventory_file)

    all_tools = {tool["name"] for tool in inventory["tools"]}
    summary = FixtureSerializer(str(fixture_dir)).export_summary()
    covered_tools = set(summary["tool_coverage"])
    missing = sorted(all_tools - covered_tools)

    print(f"Total tools: {len(all_tools)}")
    print(f"Covered tools: {len(covered_tools)}")
    print(f"Missing coverage: {len(missing)}")

    if missing:
        print("Missing tools:")
        for tool_name in missing:
            print(f"  - {tool_name}")
        return 1

    print("All inventoried tools have fixture coverage.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=Path(__file__).parent / "fixtures",
        help="Directory containing recorded JSONL fixtures",
    )
    args = parser.parse_args()
    inventory_path = Path(__file__).parent / "tool_inventory.json"
    return verify_fixture_coverage(args.fixture_dir, inventory_path)


if __name__ == "__main__":
    raise SystemExit(main())
