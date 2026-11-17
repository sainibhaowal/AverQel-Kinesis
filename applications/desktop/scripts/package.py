#!/usr/bin/env python3
"""
AverQel Kinesis Desktop Packaging Script

This script orchestrates the complete desktop application build process:
1. Builds the backend with PyInstaller (including alembic migrations)
2. Builds the frontend
3. Prepares brand assets
4. Runs electron-builder for cross-platform packaging
5. Validates the packaged application

Usage:
    python scripts/package.py [--platform linux|windows|macos|all] [--arch x64|arm64]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal


class DesktopPackager:
    """Orchestrates the desktop application build process."""

    def __init__(
        self,
        platform: Literal["linux", "windows", "macos", "all"] = "all",
        arch: Literal["x64", "arm64"] = "x64",
        skip_frontend: bool = False,
        skip_backend: bool = False,
        skip_tests: bool = False,
    ) -> None:
        self.platform = platform
        self.arch = arch
        self.skip_frontend = skip_frontend
        self.skip_backend = skip_backend
        self.skip_tests = skip_tests

        self.desktop_root = Path(__file__).resolve().parent.parent
        self.repo_root = self.desktop_root.parent.parent
        self.backend_root = self.desktop_root.parent.parent / "backend"
        self.frontend_root = self.repo_root / "frontend"
        self.scripts_dir = self.desktop_root / "scripts"
        self.assets_dir = self.desktop_root / "assets"
        self.dist_dir = self.desktop_root / "release"

    async def run(self) -> int:
        """Run the complete packaging process."""
        print(f"🚀 Starting AverQel Kinesis Desktop Packaging")
        print(f"   Platform: {self.platform}")
        print(f"   Architecture: {self.arch}")
        print(f"   Repository root: {self.repo_root}")

        try:
            await self._validate_environment()
            await self._prepare_directories()

            if not self.skip_backend:
                await self._build_backend()

            if not self.skip_frontend:
                await self._build_frontend()

            await self._prepare_assets()
            await self._run_electron_builder()

            if not self.skip_tests:
                await self._run_smoke_tests()

            print("✅ Packaging completed successfully!")
            print(f"📦 Artifacts available in: {self.dist_dir}")
            return 0

        except Exception as e:
            print(f"❌ Packaging failed: {e}")
            return 1

    async def _validate_environment(self) -> None:
        """Validate build environment prerequisites."""
        print("\n🔍 Validating build environment...")

        # Check Node.js version
        node_version = await self._run_command(["node", "--version"])
        print(f"   Node.js: {node_version.strip()}")

        # Check pnpm
        pnpm_version = await self._run_command(["pnpm", "--version"])
        print(f"   pnpm: {pnpm_version.strip()}")

        # Check Python version
        python_version = await self._run_command(["python3.12", "--version"])
        print(f"   Python: {python_version.strip()}")

        # Check PyInstaller
        try:
            pyinstaller_version = await self._run_command(["python3.12", "-m", "PyInstaller", "--version"])
            print(f"   PyInstaller: {pyinstaller_version.strip()}")
        except subprocess.CalledProcessError:
            print("   ⚠️  PyInstaller not available, will install via build script")

        # Check electron-builder
        try:
            eb_version = await self._run_command(["npx", "electron-builder", "--version"])
            print(f"   electron-builder: {eb_version.strip()}")
        except subprocess.CalledProcessError:
            print("   ⚠️  electron-builder not available")

        # Check platform-specific tools
        if self.platform in ("windows", "all") and platform.system() == "Windows":
            await self._check_windows_tools()
        elif self.platform in ("macos", "all") and platform.system() == "Darwin":
            await self._check_macos_tools()
        elif self.platform in ("linux", "all") and platform.system() == "Linux":
            await self._check_linux_tools()

    async def _check_windows_tools(self) -> None:
        """Check Windows-specific build tools."""
        tools = {
            "makensis": "NSIS",
            "signtool": "SignTool (optional)",
        }
        for tool, name in tools.items():
            try:
                await self._run_command([tool, "/?"])
                print(f"   ✅ {name}: available")
            except (subprocess.CalledProcessError, FileNotFoundError):
                print(f"   ⚠️  {name}: not found ({tool})")

    async def _check_macos_tools(self) -> None:
        """Check macOS-specific build tools."""
        tools = {
            "hdiutil": "hdiutil",
            "codesign": "codesign",
            "xcrun": "xcrun",
            "security": "security",
            "notarytool": "notarytool (optional)",
        }
        for tool, name in tools.items():
            try:
                await self._run_command([tool, "-h"])
                print(f"   ✅ {name}: available")
            except (subprocess.CalledProcessError, FileNotFoundError):
                print(f"   ⚠️  {name}: not found ({tool})")

    async def _check_linux_tools(self) -> None:
        """Check Linux-specific build tools."""
        tools = {
            "dpkg-deb": "dpkg-deb",
            "rpmbuild": "rpmbuild",
            "fakeroot": "fakeroot",
            "desktop-file-validate": "desktop-file-validate",
            "appimagetool": "appimagetool",
        }
        for tool, name in tools.items():
            try:
                await self._run_command([tool, "--version"])
                print(f"   ✅ {name}: available")
            except (subprocess.CalledProcessError, FileNotFoundError):
                print(f"   ⚠️  {name}: not found ({tool})")

    async def _prepare_directories(self) -> None:
        """Prepare build directories."""
        print("\n📁 Preparing build directories...")
        dirs = [
            self.desktop_root / "build",
            self.desktop_root / "dist",
            self.desktop_root / "release",
            self.backend_root / "build",
            self.backend_root / "dist",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            print(f"   Created: {d.relative_to(self.repo_root)}")

    async def _build_backend(self) -> int:
        """Build the backend with PyInstaller."""
        print("\n🔨 Building backend with PyInstaller...")

        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(self.backend_root),
            "PYTHONNOUSERSITE": "1",
        })

        # Run the build script
        result = await self._run_command(
            ["node", "scripts/build-backend.mjs"],
            cwd=self.desktop_root,
            env={**os.environ, **{"PYTHONPATH": str(self.backend_root)}}
        )

        if result.returncode != 0:
            raise RuntimeError("Backend build failed")

        # Verify the executable was created
        if platform.system() == "Windows":
            exe_path = self.backend_root / "dist" / "averqel-backend" / "averqel-backend.exe"
        else:
            exe_path = self.backend_root / "dist" / "averqel-backend" / "averqel-backend"

        if not exe_path.exists():
            raise RuntimeError(f"Backend executable not found at {exe_path}")

        print(f"   ✅ Backend executable: {exe_path}")
        return 0

    async def _build_frontend(self) -> int:
        """Build the frontend."""
        print("\n🔨 Building frontend...")

        result = await self._run_command(
            ["pnpm", "run", "build"],
            cwd=self.frontend_root
        )

        if result.returncode != 0:
            raise RuntimeError("Frontend build failed")

        # Verify frontend output
        frontend_out = self.frontend_root / "out"
        if not frontend_out.exists():
            raise RuntimeError("Frontend output directory not found")

        print(f"   ✅ Frontend built: {frontend_out}")
        return 0

    async def _prepare_assets(self) -> None:
        """Prepare brand assets and ensure all required assets exist."""
        print("\n🎨 Preparing brand assets...")

        result = await self._run_command(
            ["node", "scripts/prepare-brand-assets.mjs"],
            cwd=self.desktop_root
        )

        if result.returncode != 0:
            raise RuntimeError("Asset preparation failed")

        # Verify required assets
        required_assets = [
            self.assets_dir / "icon.ico",
            self.assets_dir / "icon.icns",
            self.assets_dir / "icon.png",
            self.assets_dir / "icon.icns",
            self.assets_dir / "LICENSE",
        ]

        for asset in required_assets:
            if not asset.exists():
                print(f"   ⚠️  Missing asset: {asset.relative_to(self.desktop_root)}")
            else:
                print(f"   ✅ {asset.relative_to(self.desktop_root)}")

    async def _run_electron_builder(self) -> int:
        """Run electron-builder for cross-platform packaging."""
        print("\n📦 Running electron-builder...")

        # Determine target platforms
        targets = []
        if self.platform == "all":
            targets = ["linux", "win32", "darwin"]
        elif self.platform == "linux":
            targets = ["linux"]
        elif self.platform == "windows":
            targets = ["win32"]
        elif self.platform == "macos":
            targets = ["darwin"]

        # Build target string for electron-builder
        target_args = []
        for target in targets:
            if target == "linux":
                target_args.extend(["--linux", "deb", "rpm", "AppImage"])
            elif target == "win32":
                target_args.extend(["--win", "nsis"])
            elif target == "darwin":
                target_args.extend(["--mac", "dmg"])

        # Architecture
        arch_args = ["--arch", self.arch]

        # Run electron-builder
        cmd = [
            "npx", "electron-builder",
            *target_args,
            *arch_args,
            "--publish", "never",
            "--config", "package.json",
        ]

        print(f"   Running: {' '.join(cmd)}")

        result = await self._run_command(cmd, cwd=self.desktop_root)

        if result.returncode != 0:
            raise RuntimeError("electron-builder failed")

        print(f"   ✅ electron-builder completed")
        return 0

    async def _run_smoke_tests(self) -> int:
        """Run smoke tests on the packaged application."""
        print("\n🧪 Running smoke tests...")

        # Find the built application
        app_paths = list(self.dist_dir.rglob("*.exe")) if platform.system() == "Windows" else \
                    list(self.dist_dir.rglob("*.dmg")) if platform.system() == "Darwin" else \
                    list(self.dist_dir.rglob("*.AppImage")) + list(self.dist_dir.rglob("*.deb")) + list(self.dist_dir.rglob("*.rpm"))

        if not app_paths:
            print("   ⚠️  No packaged application found, skipping smoke tests")
            return 0

        app_path = app_paths[0]
        print(f"   Testing: {app_path}")

        result = await self._run_command(
            ["node", "scripts/smoke-packaged-backend.cjs", str(app_path)],
            cwd=self.desktop_root
        )

        if result.returncode != 0:
            print("   ⚠️  Smoke tests failed (non-blocking)")
        else:
            print("   ✅ Smoke tests passed")

        return 0

    async def _run_command(
        self,
        cmd: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a command and return the result."""
        print(f"   $ {' '.join(cmd)}")
        return await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=cwd or self.desktop_root,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour timeout
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="AverQel Kinesis Desktop Packager")
    parser.add_argument(
        "--platform",
        choices=["linux", "windows", "macos", "all"],
        default="all",
        help="Target platform(s) to build for",
    )
    parser.add_argument(
        "--arch",
        choices=["x64", "arm64"],
        default="x64",
        help="Target architecture",
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip frontend build",
    )
    parser.add_argument(
        "--skip-backend",
        action="store_true",
        help="Skip backend build",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip smoke tests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without executing",
    )

    args = parser.parse_args()

    packager = DesktopPackager(
        platform=args.platform,
        arch=args.arch,
        skip_frontend=args.skip_frontend,
        skip_backend=args.skip_backend,
        skip_tests=args.skip_tests,
    )

    if args.dry_run:
        print("DRY RUN - would execute packaging with:")
        print(f"  Platform: {args.platform}")
        print(f"  Arch: {args.arch}")
        print(f"  Skip frontend: {args.skip_frontend}")
        print(f"  Skip backend: {args.skip_backend}")
        print(f"  Skip tests: {args.skip_tests}")
        return 0

    return asyncio.run(packager.run())


if __name__ == "__main__":
    sys.exit(main())