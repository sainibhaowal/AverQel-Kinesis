"""Session format migration chain for DeepSpace.

Provides versioned event payload migrations (v1 -> v2 -> v3 -> v4).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionFormatMigrationConfig:
    """Session format migration configuration."""

    enabled: bool = True
    current_version: int = 4
    auto_migrate_on_read: bool = True
    backup_before_migrate: bool = True


# ──────────────────────────────────────────────────────────────────────
# Migration Base
# ──────────────────────────────────────────────────────────────────────


class SessionFormatMigration(ABC):
    """Base class for a session format migration step."""

    from_version: int
    to_version: int
    description: str = ""

    @abstractmethod
    def migrate(self, event: dict[str, Any]) -> dict[str, Any]:
        """Migrate one event from from_version to to_version."""
        pass

    @abstractmethod
    def rollback(self, event: dict[str, Any]) -> dict[str, Any]:
        """Rollback one event from to_version to from_version."""
        pass


# ──────────────────────────────────────────────────────────────────────
# Migration Implementations
# ──────────────────────────────────────────────────────────────────────


class V1ToV2Migration(SessionFormatMigration):
    """v1 -> v2: Add subagent_id field to events."""

    from_version = 1
    to_version = 2
    description = "Add subagent_id field for delegation tracking"

    def migrate(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        if "subagent_id" not in event:
            event["subagent_id"] = None
        event["format_version"] = self.to_version
        return event

    def rollback(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        event.pop("subagent_id", None)
        event["format_version"] = self.from_version
        return event


class V2ToV3Migration(SessionFormatMigration):
    """v2 -> v3: Add run_id and normalize payload structure."""

    from_version = 2
    to_version = 3
    description = "Add run_id for run tracking, normalize payload"

    def migrate(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        if "run_id" not in event:
            event["run_id"] = None
        # Normalize payload to dict
        if "payload" in event and not isinstance(event["payload"], dict):
            event["payload"] = {"value": event["payload"]}
        event["format_version"] = self.to_version
        return event

    def rollback(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        event.pop("run_id", None)
        event["format_version"] = self.from_version
        return event


class V3ToV4Migration(SessionFormatMigration):
    """v3 -> v4: Add step field, separate tool call/result payloads."""

    from_version = 3
    to_version = 4
    description = "Add step field, separate tool call/result payloads"

    def migrate(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        if "step" not in event:
            event["step"] = None
        # Ensure tool call/result have structured payloads
        if event.get("event_type") in ("tool/call", "tool/result"):
            payload = event.get("payload") or {}
            if "arguments" not in payload and "input" in payload:
                payload["arguments"] = payload.pop("input")
            if "result" not in payload and "output" in payload:
                payload["result"] = payload.pop("output")
            event["payload"] = payload
        event["format_version"] = self.to_version
        return event

    def rollback(self, event: dict[str, Any]) -> dict[str, Any]:
        event = dict(event)
        event.pop("step", None)
        event["format_version"] = self.from_version
        return event


# ──────────────────────────────────────────────────────────────────────
# Migration Chain
# ──────────────────────────────────────────────────────────────────────


class SessionFormatMigrationChain:
    """Manages the chain of format migrations."""

    def __init__(self, config: SessionFormatMigrationConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self._migrations: list[SessionFormatMigration] = [
            V1ToV2Migration(),
            V2ToV3Migration(),
            V3ToV4Migration(),
        ]
        self._version_map = {m.from_version: m for m in self._migrations}

    def get_current_version(self) -> int:
        return self.config.current_version

    def get_migration(self, from_version: int) -> SessionFormatMigration | None:
        return self._version_map.get(from_version)

    def migrate_event(
        self, event: dict[str, Any], target_version: int | None = None
    ) -> dict[str, Any]:
        """Migrate one event to target version (or current)."""
        if not self.config.enabled or not self.config.auto_migrate_on_read:
            return event
        target = target_version or self.config.current_version
        current = event.get("format_version", 1)

        if current >= target:
            return event

        migrated = dict(event)
        for v in range(current, target):
            migration = self._version_map.get(v)
            if migration is None:
                logger.warning("No migration from v%d to v%d", v, v + 1)
                break
            try:
                migrated = migration.migrate(migrated)
            except Exception as e:
                logger.error("Migration v%d->v%d failed: %s", v, v + 1, e)
                break
        return migrated

    def migrate_events(
        self, events: list[dict[str, Any]], target_version: int | None = None
    ) -> list[dict[str, Any]]:
        """Migrate a list of events."""
        return [self.migrate_event(e, target_version) for e in events]

    def rollback_event(self, event: dict[str, Any], target_version: int) -> dict[str, Any]:
        """Rollback an event to a previous version."""
        current = event.get("format_version", self.config.current_version)
        if current <= target_version:
            return event

        rolled_back = dict(event)
        for v in range(current, target_version, -1):
            migration = self._version_map.get(v - 1)
            if migration is None:
                break
            try:
                rolled_back = migration.rollback(rolled_back)
            except Exception as e:
                logger.error("Rollback v%d->v%d failed: %s", v, v - 1, e)
                break
        return rolled_back


# ──────────────────────────────────────────────────────────────────────
# Service Factory
# ──────────────────────────────────────────────────────────────────────

_FORMAT_MIGRATION: SessionFormatMigrationChain | None = None


def get_format_migration_chain(settings: Settings | None = None) -> SessionFormatMigrationChain:
    """Get or create the global format migration chain."""
    global _FORMAT_MIGRATION
    if _FORMAT_MIGRATION is None:
        cfg = SessionFormatMigrationConfig(
            enabled=getattr(settings or get_settings(), "deepspace_format_migration_enabled", True),
            current_version=getattr(
                settings or get_settings(), "deepspace_format_current_version", 4
            ),
            auto_migrate_on_read=getattr(
                settings or get_settings(), "deepspace_format_auto_migrate", True
            ),
            backup_before_migrate=getattr(
                settings or get_settings(), "deepspace_format_backup", True
            ),
        )
        _FORMAT_MIGRATION = SessionFormatMigrationChain(cfg, settings or get_settings())
    return _FORMAT_MIGRATION


def init_format_migration_chain(settings: Settings) -> SessionFormatMigrationChain:
    """Initialize the format migration chain."""
    global _FORMAT_MIGRATION
    cfg = SessionFormatMigrationConfig(
        enabled=getattr(settings, "deepspace_format_migration_enabled", True),
        current_version=getattr(settings, "deepspace_format_current_version", 4),
        auto_migrate_on_read=getattr(settings, "deepspace_format_auto_migrate", True),
        backup_before_migrate=getattr(settings, "deepspace_format_backup", True),
    )
    _FORMAT_MIGRATION = SessionFormatMigrationChain(cfg, settings)
    return _FORMAT_MIGRATION


__all__ = [
    "SessionFormatMigrationConfig",
    "SessionFormatMigration",
    "SessionFormatMigrationChain",
    "V1ToV2Migration",
    "V2ToV3Migration",
    "V3ToV4Migration",
    "get_format_migration_chain",
    "init_format_migration_chain",
]
