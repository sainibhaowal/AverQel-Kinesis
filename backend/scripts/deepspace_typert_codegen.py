#!/usr/bin/env python3
"""Generate or verify the Python-native Typert artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402 - backend path is added above
from app.deepspace.typert.codegen import (  # noqa: E402 - backend path is added above
    check_artifacts,
    render_artifact_tree,
    write_artifacts,
)
from app.deepspace.typert.service import get_typert_service  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="artifact output directory")
    parser.add_argument("--package", action="append", dest="packages")
    parser.add_argument("--face", default="host", choices=("host", "client"))
    parser.add_argument(
        "--check", action="store_true", help="report missing/stale files without writing"
    )
    args = parser.parse_args(argv)

    service = get_typert_service()
    service.start(get_settings())
    available = sorted(
        {record.package for record in service.registry.list_packages(face=args.face)}
    )
    packages = sorted(set(args.packages or available))
    unknown = sorted(set(packages) - set(available))
    if unknown:
        parser.error(f"unknown {args.face} package(s): {', '.join(unknown)}")

    artifacts = [
        artifact
        for package in packages
        for artifact in render_artifact_tree(
            service.registry,
            package,
            face=args.face,
        )
    ]
    if args.check:
        problems = check_artifacts(artifacts, args.output)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        print(f"Typert artifacts are current ({len(artifacts)} files).")
        return 0

    written = write_artifacts(artifacts, args.output)
    print(f"Generated {len(written)} Python-native Typert artifacts in {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
