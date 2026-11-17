#!/usr/bin/env python3
"""
AverQel Kinesis Backup Script

This script creates a complete backup of the AverQel Kinesis installation,
including:
- SQLite database
- Workspace files
- Configuration files
- User data

Usage:
    python scripts/backup.py [--output-dir OUTPUT_DIR] [--compress] [--verbose]
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import shutil
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add the backend to the path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.averqel_home import get_averqel_home
from app.core.config import Settings, get_settings
from app.platform.database.session import get_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class BackupManager:
    """Manages backup operations for AverQel Kinesis."""

    def __init__(self, output_dir: Path | None = None, compress: bool = True) -> None:
        self.settings = get_settings()
        self.home = get_averqel_home()
        self.output_dir = output_dir or (self.home / "backups")
        self.compress = compress
        self.timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        self.backup_name = f"averqel-backup-{self.timestamp}"
        self.backup_dir = self.output_dir / self.backup_name
        self.metadata = {
            "version": "1.0",
            "created_at": datetime.now(UTC).isoformat(),
            "hostname": __import__("socket").gethostname(),
            "platform": __import__("platform").platform(),
            "python_version": __import__("sys").version.split()[0],
        }

    def run(self) -> Path:
        """Run the complete backup process."""
        logger.info("Starting backup: %s", self.backup_name)
        start_time = time.time()

        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)

            # Backup components
            self._backup_database()
            self._backup_workspace()
            self._backup_configuration()
            self._backup_user_data()

            # Create metadata
            self.metadata["duration_seconds"] = time.time() - start_time
            self.metadata["completed_at"] = datetime.now(UTC).isoformat()
            self._write_metadata()

            # Compress if requested
            if self.compress:
                archive_path = self._compress_backup()
                # Remove uncompressed directory
                shutil.rmtree(self.backup_dir)
                logger.info("Backup completed: %s (%.2f seconds)", archive_path, time.time() - start_time)
                return archive_path
            else:
                logger.info("Backup completed: %s (%.2f seconds)", self.backup_dir, time.time() - start_time)
                return self.backup_dir

        except Exception as exc:
            logger.error("Backup failed: %s", exc, exc_info=True)
            # Cleanup on failure
            if self.backup_dir.exists():
                shutil.rmtree(self.backup_dir, ignore_errors=True)
            raise

    def _backup_database(self) -> None:
        """Backup the SQLite database."""
        logger.info("Backing up database...")
        db_dir = self.backup_dir / "database"
        db_dir.mkdir(parents=True, exist_ok=True)

        # Find database files
        db_files = list(get_averqel_home().glob("*.db"))
        db_files.extend(get_averqel_home().glob("db/*.db"))

        for db_file in db_files:
            if db_file.exists():
                dest = db_dir / db_file.relative_to(get_averqel_home())
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(db_file, dest)
                logger.info("  Backed up database: %s", db_file.name)

        # Also backup alembic migrations
        alembic_dir = get_averqel_home() / "alembic"
        if alembic_dir.exists():
            shutil.copytree(alembic_dir, db_dir / "alembic", dirs_exist_ok=True)
            logger.info("  Backed up alembic migrations")

    def _backup_workspace(self) -> None:
        """Backup workspace files."""
        logger.info("Backing up workspace...")
        ws_dir = self.backup_dir / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)

        workspace_root = get_averqel_home()
        # Backup workspace files (excluding large directories)
        exclude_patterns = {
            ".git",
            "__pycache__",
            ".pyc",
            ".pytest_cache",
            ".mypy_cache",
            ".venv",
            "venv",
            "env",
            ".venv",
            "node_modules",
            ".git",
            ".idea",
            ".vscode",
            "*.log",
            "*.tmp",
            "*.temp",
            "*.cache",
            "__pycache__",
        }

        def should_ignore(path: Path) -> bool:
            for pattern in exclude_patterns:
                if pattern in path.name or pattern in str(path):
                    return True
                if path.name.endswith(pattern.replace("*", "")):
                    return True
            return False

        copied = 0
        for src in workspace_root.rglob("*"):
            if should_ignore(src):
                continue
            if src.is_file():
                try:
                    rel = src.relative_to(get_averqel_home())
                    dst = ws_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
                except (OSError, ValueError) as exc:
                    logger.warning("Failed to copy %s: %s", src, exc)

        logger.info("  Backed up %d workspace files", copied)

    def _backup_configuration(self) -> None:
        """Backup configuration files."""
        logger.info("Backing up configuration...")
        cfg_dir = self.backup_dir / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)

        # Backup key configuration files
        config_files = [
            "alembic.ini",
            "requirements.txt",
            "requirements-build.txt",
            ".env",
            ".env.local",
        ]

        for config_file in config_files:
            src = get_averqel_home() / config_file
            if src.exists():
                shutil.copy2(src, cfg_dir / config_file)
                logger.info("  Backed up config: %s", config_file)

        # Backup electron assets if they exist
        electron_assets = [
            "assets/icon.ico",
            "assets/icon.icns",
            "assets/icon.png",
            "assets/entitlements.mac.plist",
            "assets/installer.nsi",
        ]

        for asset in electron_assets:
            src = Path("applications/desktop") / asset
            if src.exists():
                dst = cfg_dir / asset
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    def _backup_user_data(self) -> None:
        """Backup user-specific data."""
        logger.info("Backing up user data...")
        user_dir = self.backup_dir / "user_data"
        user_dir.mkdir(parents=True, exist_ok=True)

        # Backup user-specific directories
        user_dirs = [
            "cache",
            "logs",
            "storage",
        ]

        for dir_name in user_dirs:
            src = get_averqel_home() / dir_name
            if src.exists():
                dst = user_dir / dir_name
                try:
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    logger.info("  Backed up: %s", dir_name)
                except OSError as exc:
                    logger.warning("Failed to backup %s: %s", dir_name, exc)

    def _write_metadata(self) -> None:
        """Write backup metadata."""
        meta_file = self.backup_dir / "backup_metadata.json"
        meta_file.write_text(json.dumps(self.metadata, indent=2), encoding="utf-8")
        logger.info("  Wrote metadata: %s", meta_file)

    def _compress_backup(self) -> Path:
        """Compress the backup directory into a tar.gz archive."""
        logger.info("Compressing backup...")
        archive_name = f"{self.backup_name}.tar.gz"
        archive_path = self.output_dir / archive_name

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(self.backup_dir, arcname=self.backup_name)

        logger.info("  Compressed to: %s (%.2f MB)", archive_path, archive_path.stat().st_size / 1024 / 1024)
        return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description="AverQel Kinesis Backup Utility")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory for backup (default: ~/AverQel/backups)",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="Do not compress the backup",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        manager = BackupManager(
            output_dir=args.output_dir,
            compress=not args.no_compress,
        )
        backup_path = manager.run()
        print(f"\n✅ Backup completed successfully: {backup_path}")
        return 0
    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())