"""Write a platform-specific manifest for the active DeepSpace Python runtime."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.deepspace.services.runtime_manifest import write_runtime_manifest
from app.deepspace.services.tools.dependencies import WORKSPACE_LIBRARIES


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Destination runtime.json path")
    parser.add_argument(
        "--root",
        type=Path,
        help="Root of the already assembled runtime payload (defaults to the manifest directory)",
    )
    args = parser.parse_args()
    write_runtime_manifest(args.path, WORKSPACE_LIBRARIES, root=args.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
