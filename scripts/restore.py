#!/usr/bin/env python3
"""
AverQel Kinesis Restore Script

This script restores a backup created by the backup.py script.

Usage:
    python scripts/restore.py BACKUP_PATH [--force] [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add the backend to the path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.averqel_home import get_averqel_home

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class RestoreManager:
    """Manages restore operations for AverQel Kinesis."""

    def __init__(
        self,
        backup_path: Path,
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.backup_path = Path(backup_path).resolve()
        self.force = force
        self.dry_run = dry_run
        self.home = get_averqel_home()
        self.metadata: dict[str, Any] = {}
        self.temp_dir: Path | None = None

    def run(self) -> int:
        """Run the complete restore process."""
        logger.info("Starting restore from: %s", self.backup_path)

        try:
            self._validate_backup()
            self._extract_backup()
            self._verify_metadata()

            if not self.dry_run:
                if not self.force:
                    confirm = input(
                        f"This will overwrite existing data in {get_averqel_home()}. "
                        "Are you sure? (yes/no): "
                    )
                    if confirm.lower() != "yes":
                        logger.info("Restore cancelled by user")
                        return 1

                self._restore_database()
                self._restore_workspace()
                self._restore_configuration()
                self._restore_user_data()
                logger.info("✅ Restore completed successfully")
            else:
                logger.info("Dry run completed - no changes made")

            return 0

        except Exception as exc:
            logger.error("Restore failed: %s", exc, exc_info=True)
            return 1
        finally:
            self._cleanup()

    def _validate_backup(self) -> None:
        """Validate the backup file exists and is readable."""
        logger.info("Validating backup: %s", self.backup_path)

        if not self.backup_path.exists():
            raise FileNotFoundError(f"Backup file not found: {self.backup_path}")

        if not self.backup_path.is_file():
            raise ValueError(f"Backup path is not a file: {self.backup_path}")

        # Check if it's a valid tar.gz
        if not tarfile.is_tarfile(self.backup_path):
            raise ValueError("Backup file is not a valid tar archive")

        logger.info("✓ Backup file validated")

    def _extract_backup(self) -> None:
        """Extract the backup archive to a temporary directory."""
        logger.info("Extracting backup...")

        self.temp_dir = Path(tempfile.mkdtemp(prefix="averqel-restore-"))

        with tarfile.open(self.backup_path, "r:gz") as tar:
            tar.extractall(self.temp_dir)

        # Find the backup directory (should be the only directory)
        contents = list(self.temp_dir.iterdir())
        if len(contents) != 1 or not contents[0].is_dir():
            raise ValueError("Backup archive does not contain a single root directory")

        self.backup_root = contents[0]
        logger.info("✓ Backup extracted to: %s", self.backup_root)

    def _verify_metadata(self) -> None:
        """Verify backup metadata."""
        meta_file = self.backup_root / "backup_metadata.json"
        if not meta_file.exists():
            logger.warning("No metadata file found in backup")
            return

        with meta_file.open("r", encoding="utf-8") as f:
            self.metadata = json.load(f)

        logger.info("Backup metadata:")
        logger.info("  Version: %s", self.metadata.get("version", "unknown"))
        logger.info("  Created: %s", self.metadata.get("created_at", "unknown"))
        logger.info("  Hostname: %s", self.metadata.get("hostname", "unknown"))
        logger.info("  Platform: %s", self.metadata.get("platform", "unknown"))

    def _restore_database(self) -> None:
        """Restore database files."""
        logger.info("Restoring database...")

        db_src = self.backup_root / "database"
        if not db_src.exists():
            logger.warning("No database backup found")
            return

        # Stop the backend if running (in a real implementation)
        # For now, we just copy the files

        db_dest = get_averqel_home()
        db_src_files = list(db_src.glob("**/*.db"))

        for db_file in db_src_files:
            if db_file.is_file():
                rel_path = db_file.relative_to(self.backup_root / "database")
                dest = db_dest / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)

                if dest.exists() and not self.force:
                    raise RuntimeError(
                        f"Database file {dest} already exists. Use --force to overwrite."
                    )

                shutil.copy2(db_file, dest)
                logger.info("  Restored database: %s", rel_path)

        # Restore alembic
        alembic_src = self.backup_root / "database" / "alembic"
        if alembic_src.exists():
            alembic_dest = get_averqel_home() / "alembic"
            if alembic_dest.exists():
                shutil.rmtree(alembic_dest)
            shutil.copytree(alembic_src, alembic_dest)
            logger.info("  Restored alembic migrations")

    def _restore_workspace(self) -> None:
        """Restore workspace files."""
        logger.info("Restoring workspace...")

        ws_src = self.backup_root / "workspace"
        if not ws_src.exists():
            logger.warning("No workspace backup found")
            return

        ws_dest = get_averqel_home()

        # Copy workspace files
        for src in ws_src.rglob("*"):
            if src.is_file():
                try:
                    rel = src.relative_to(ws_src)
                    dest = ws_dest / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    if dest.exists() and not self.force:
                        logger.warning("  Skipping existing file: %s", rel)
                        continue

                    shutil.copy2(src, dest)
                except (OSError, ValueError) as exc:
                    logger.warning("Failed to restore %s: %s", src, exc)

        logger.info("✓ Workspace restored")

    def _restore_configuration(self) -> None:
        """Restore configuration files."""
        logger.info("Restoring configuration...")

        cfg_src = self.backup_root / "config"
        if not cfg_src.exists():
            logger.warning("No configuration backup found")
            return

        # Restore config files
        for config_file in cfg_src.iterdir():
            if config_file.is_file():
                dest = get_averqel_home() / config_file.name
                if dest.exists() and not self.force:
                    logger.warning("  Skipping existing config: %s", config_file.name)
                    continue

                shutil.copy2(config_file, dest)
                logger.info("  Restored config: %s", config_file.name)

        # Restore alembic.ini if present
        alembic_ini = cfg_src / "alembic.ini"
        if alembic_ini.exists():
            shutil.copy2(alembic_ini, get_averqel_home() / "alembic.ini")
            logger.info("  Restored alembic.ini")

    def _restore_user_data(self) -> None:
        """Restore user data."""
        logger.info("Restoring user data...")

        user_src = self.backup_root / "user_data"
        if not user_src.exists():
            logger.warning("No user data backup found")
            return

        for item in user_src.iterdir():
            if item.is_dir():
                dest = get_averqel_home() / item.name
                if dest.exists() and not self.force:
                    logger.warning("  Skipping existing directory: %s", item.name)
                    continue

                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
                logger.info("  Restored user data: %s", item.name)

    def _cleanup(self) -> None:
        """Clean up temporary files."""
        if self.temp_dir and self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir)
                logger.debug("Cleaned up temporary directory: %s", self.temp_dir)
            except OSError as exc:
                logger.warning("Failed to clean up temp dir: %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="AverQel Kinesis Restore Utility")
    parser.add_argument("backup_path", type=Path, help="Path to backup archive (.tar.gz)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files without prompting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate backup and show what would be restored without making changes",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    manager = RestoreManager(
        backup_path=args.backup_path,
        force=args.force,
        dry_run=args.dry_run,
    )

    return manager.run()


if __name__ == "__main__":
    import tempfile
    sys.exit(main())