#!/usr/bin/env python3
"""Select supported native desktop build targets for GitHub Actions."""

from __future__ import annotations

import json
import os
from pathlib import Path

TARGETS = (
    {
        "platform": "linux",
        "arch": "x64",
        "runner": "ubuntu-24.04",
        "build_script": "build:linux:x64",
    },
    {
        "platform": "linux",
        "arch": "arm64",
        "runner": "ubuntu-24.04-arm",
        "build_script": "build:linux:arm64",
    },
    {
        "platform": "windows",
        "arch": "x64",
        "runner": "windows-2025",
        "build_script": "build:windows:x64",
    },
    {
        "platform": "windows",
        "arch": "arm64",
        "runner": "windows-11-arm",
        "build_script": "build:windows:arm64",
    },
    {
        "platform": "macos",
        "arch": "x64",
        "runner": "macos-15-intel",
        "build_script": "build:mac:x64",
    },
    {
        "platform": "macos",
        "arch": "arm64",
        "runner": "macos-15",
        "build_script": "build:mac:arm64",
    },
)


def select_targets(event_name: str, platform: str, arch: str) -> list[dict[str, str]]:
    """Return all release targets or the requested manual-dispatch subset."""
    if event_name == "push":
        return list(TARGETS)
    if event_name != "workflow_dispatch":
        raise ValueError(f"Unsupported workflow event: {event_name}")

    valid_platforms = {"all", *(target["platform"] for target in TARGETS)}
    valid_architectures = {"all", *(target["arch"] for target in TARGETS)}
    if platform not in valid_platforms:
        raise ValueError(f"Unsupported platform selection: {platform}")
    if arch not in valid_architectures:
        raise ValueError(f"Unsupported architecture selection: {arch}")

    selected = [
        target
        for target in TARGETS
        if (platform == "all" or target["platform"] == platform)
        and (arch == "all" or target["arch"] == arch)
    ]
    if not selected:
        raise ValueError(f"No supported desktop targets match {platform}/{arch}.")
    return selected


def main() -> None:
    targets = select_targets(
        os.environ.get("EVENT_NAME", ""),
        os.environ.get("PLATFORM", ""),
        os.environ.get("ARCH", ""),
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise SystemExit(
            "GITHUB_OUTPUT is required when selecting Actions build targets."
        )
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"matrix={json.dumps(targets, separators=(',', ':'))}\n")
    print(f"Selected {len(targets)} desktop target(s).")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
