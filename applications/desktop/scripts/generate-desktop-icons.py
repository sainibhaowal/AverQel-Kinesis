#!/usr/bin/env python3
"""Regenerate desktop icon formats from the canonical AntiAtom SVG."""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = DESKTOP_ROOT.parents[1]
SOURCE_SVG = WORKSPACE_ROOT / "frontend/public/brand/AntiAtom_Icon.svg"
ASSET_ROOT = DESKTOP_ROOT / "assets"
ICNS_SIZES = {
    "icp4": 16,
    "icp5": 32,
    "icp6": 64,
    "ic07": 128,
    "ic08": 256,
    "ic09": 512,
    "ic10": 1024,
}


def main() -> None:
    magick = shutil.which("magick")
    if not magick:
        raise SystemExit("ImageMagick is required to regenerate desktop icons (missing `magick`).")
    if not SOURCE_SVG.is_file():
        raise SystemExit(f"Canonical AntiAtom SVG not found: {SOURCE_SVG}")

    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="averqel-icons-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        master_png = temporary_root / "antiatom-1024.png"
        fitted_png = temporary_root / "antiatom-icon-1024.png"
        subprocess.run(
            [
                magick,
                "-background",
                "none",
                "-density",
                "384",
                str(SOURCE_SVG),
                "-resize",
                "1024x1024",
                "-depth",
                "8",
                "-strip",
                f"PNG32:{master_png}",
            ],
            check=True,
        )
        subprocess.run(
            [
                magick,
                str(master_png),
                "-trim",
                "+repage",
                "-resize",
                "1024x860!",
                "-gravity",
                "center",
                "-background",
                "none",
                "-extent",
                "1024x1024",
                "-depth",
                "8",
                "-strip",
                f"PNG32:{fitted_png}",
            ],
            check=True,
        )

        subprocess.run(
            [
                magick,
                str(fitted_png),
                "-resize",
                "512x512",
                "-depth",
                "8",
                "-strip",
                f"PNG32:{ASSET_ROOT / 'icon.png'}",
            ],
            check=True,
        )
        subprocess.run(
            [
                magick,
                str(fitted_png),
                "-define",
                "icon:auto-resize=256,128,64,48,32,16",
                str(ASSET_ROOT / "icon.ico"),
            ],
            check=True,
        )

        chunks: list[bytes] = []
        for chunk_type, size in ICNS_SIZES.items():
            chunk_png = temporary_root / f"antiatom-{size}.png"
            subprocess.run(
                [
                    magick,
                    str(fitted_png),
                    "-resize",
                    f"{size}x{size}",
                    "-depth",
                    "8",
                    "-strip",
                    f"PNG32:{chunk_png}",
                ],
                check=True,
            )
            png_data = chunk_png.read_bytes()
            chunks.append(chunk_type.encode("ascii") + struct.pack(">I", len(png_data) + 8) + png_data)

        icns_payload = b"".join(chunks)
        icns_data = b"icns" + struct.pack(">I", len(icns_payload) + 8) + icns_payload
        (ASSET_ROOT / "icon.icns").write_bytes(icns_data)

    print("Generated icon.png, icon.ico, and icon.icns from AntiAtom_Icon.svg.")


if __name__ == "__main__":
    main()
