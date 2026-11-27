"""Tests for file permissions management."""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.deepspace.services.file_permissions import (
    FileAccessDecision,
    FileAccessPermission,
    FileAccessScope,
    FilePermissionStore,
    get_file_permission_store,
    permission_key,
)


class TestFileAccessDecision:
    """Tests for the FileAccessDecision enum."""

    def test_enum_values(self) -> None:
        assert FileAccessDecision.ALLOW.value == "allow"
        assert FileAccessDecision.DENY.value == "deny"
        assert FileAccessDecision.ALWAYS_ALLOW.value == "always_allow"
        assert FileAccessDecision.ALWAYS_DENY.value == "always_deny"


class TestFileAccessScope:
    """Tests for the FileAccessScope enum."""

    def test_enum_values(self) -> None:
        assert FileAccessScope.WORKSPACE.value == "workspace"
        assert FileAccessScope.HOME.value == "home"
        assert FileAccessScope.SYSTEM.value == "system"
        assert FileAccessScope.EXTERNAL.value == "external"


class TestFileAccessPermission:
    """Tests for the FileAccessPermission dataclass."""

    def test_matches_exact_path(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read", "write"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        assert permission.matches("/home/user/file.txt", "read") is True
        assert permission.matches("/home/user/file.txt", "write") is True
        assert permission.matches("/home/user/file.txt", "delete") is False
        assert permission.matches("/home/user/other.txt", "read") is False

    def test_matches_glob_pattern(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/*.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        assert permission.matches("/home/user/file.txt", "read") is True
        assert permission.matches("/home/user/document.txt", "read") is True
        assert permission.matches("/home/user/file.py", "read") is False

    def test_matches_directory_pattern(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/projects",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read", "list"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        assert permission.matches("/home/user/projects", "read") is True
        assert permission.matches("/home/user/projects/file.txt", "read") is True
        assert permission.matches("/home/user/projects/subdir/file.txt", "read") is True
        assert permission.matches("/home/user/other/file.txt", "read") is False

    def test_matches_wildcard_all(self) -> None:
        permission = FileAccessPermission(
            path_pattern="**",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        assert permission.matches("/home/user/file.txt", "read") is True
        assert permission.matches("/home/user/projects/file.py", "read") is True
        assert permission.matches("/any/path", "read") is True

    def test_matches_operation_not_allowed(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        assert permission.matches("/home/user/file.txt", "write") is False
        assert permission.matches("/home/user/file.txt", "delete") is False

    def test_matches_invalid_path(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/invalid/path/that/does/not/exist",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        # Should not crash on invalid paths
        result = permission.matches("/some/path", "read")
        assert result is False


class TestPermissionKey:
    """Tests for the permission_key function."""

    def test_generates_correct_key(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/*.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        key = permission_key(permission)
        assert key == "home:/home/user/*.txt"

    def test_different_scopes_different_keys(self) -> None:
        perm1 = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        perm2 = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.WORKSPACE,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        assert permission_key(perm1) != permission_key(perm2)


class TestFilePermissionStore:
    """Tests for the FilePermissionStore class."""

    def setup_method(self) -> None:
        # Create a temporary directory for testing
        self.temp_dir = tempfile.mkdtemp()
        self.store = FilePermissionStore(Path(self.temp_dir))

    def teardown_method(self) -> None:
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initialization(self) -> None:
        assert self.store._home == Path(self.temp_dir)
        assert self.store._permission_file == Path(self.temp_dir) / "file_permissions.json"
        assert self.store._initialized is False
        assert len(self.store._permissions) == 0

    def test_check_permission_no_permissions(self) -> None:
        result = self.store.check_permission("/home/user/file.txt", "read")
        assert result is None

    def test_check_permission_with_matching_permission(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
        )
        self.store._permissions[permission_key(permission)] = permission
        self.store._initialized = True

        result = self.store.check_permission("/home/user/file.txt", "read")
        assert result == FileAccessDecision.ALLOW

    def test_check_permission_with_deny_permission(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.DENY,
            created_at=datetime.now(UTC),
        )
        self.store._permissions[permission_key(permission)] = permission
        self.store._initialized = True

        result = self.store.check_permission("/home/user/file.txt", "read")
        assert result == FileAccessDecision.DENY

    def test_check_permission_always_allow(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALWAYS_ALLOW,
            created_at=datetime.now(UTC),
        )
        self.store._permissions[permission_key(permission)] = permission
        self.store._initialized = True

        result = self.store.check_permission("/home/user/file.txt", "read")
        assert result == FileAccessDecision.ALLOW

    def test_check_permission_always_deny(self) -> None:
        permission = FileAccessPermission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALWAYS_DENY,
            created_at=datetime.now(UTC),
        )
        self.store._permissions[permission_key(permission)] = permission
        self.store._initialized = True

        result = self.store.check_permission("/home/user/file.txt", "read")
        assert result == FileAccessDecision.DENY

    def test_grant_permission(self) -> None:
        self.store.grant_permission(
            path_pattern="/home/user/*.txt",
            scope=FileAccessScope.HOME,
            operations={"read", "write"},
            decision=FileAccessDecision.ALWAYS_ALLOW,
        )

        assert len(self.store._permissions) == 1
        key = "home:/home/user/*.txt"
        assert key in self.store._permissions
        permission = self.store._permissions[key]
        assert permission.path_pattern == "/home/user/*.txt"
        assert permission.scope == FileAccessScope.HOME
        assert permission.operations == frozenset({"read", "write"})
        assert permission.decision == FileAccessDecision.ALWAYS_ALLOW

        # Check file was saved
        assert self.store._permission_file.exists()
        content = json.loads(self.store._permission_file.read_text())
        assert content["version"] == 1
        assert len(content["permissions"]) == 1

    def test_revoke_permission_exists(self) -> None:
        self.store.grant_permission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations={"read"},
        )

        result = self.store.revoke_permission("/home/user/file.txt", FileAccessScope.HOME)
        assert result is True
        assert len(self.store._permissions) == 0

    def test_revoke_permission_not_exists(self) -> None:
        result = self.store.revoke_permission("/home/user/nonexistent.txt", FileAccessScope.HOME)
        assert result is False

    def test_list_permissions(self) -> None:
        self.store.grant_permission(
            path_pattern="/home/user/file1.txt",
            scope=FileAccessScope.HOME,
            operations={"read"},
        )
        self.store.grant_permission(
            path_pattern="/home/user/file2.txt",
            scope=FileAccessScope.HOME,
            operations={"write"},
        )

        permissions = self.store.list_permissions()
        assert len(permissions) == 2

    def test_save_load_from_disk(self) -> None:
        # Grant some permissions
        self.store.grant_permission(
            path_pattern="/home/user/*.txt",
            scope=FileAccessScope.HOME,
            operations={"read", "write"},
            decision=FileAccessDecision.ALWAYS_ALLOW,
            source="test",
        )
        self.store.grant_permission(
            path_pattern="/home/user/projects",
            scope=FileAccessScope.HOME,
            operations={"read", "list"},
            decision=FileAccessDecision.ALLOW,
            source="test",
        )

        # Create a new store with the same directory
        new_store = FilePermissionStore(Path(self.temp_dir))
        permissions = new_store.list_permissions()

        assert len(permissions) == 2
        patterns = {p.path_pattern for p in permissions}
        assert "/home/user/*.txt" in patterns
        assert "/home/user/projects" in patterns

    def test_load_from_disk_corrupted_file(self) -> None:
        # Write invalid JSON
        self.store._permission_file.write_text("not valid json")
        self.store._initialized = False

        # Should not crash, just log warning
        permissions = self.store.list_permissions()
        assert permissions == []

    def test_load_from_disk_missing_file(self) -> None:
        # File doesn't exist
        self.store._initialized = False

        permissions = self.store.list_permissions()
        assert permissions == []

    def test_load_from_disk_invalid_permission_data(self) -> None:
        # Write valid JSON but with invalid permission data
        data = {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "permissions": [
                {
                    "path_pattern": "/home/user/file.txt",
                    "scope": "home",
                    "operations": ["read"],
                    "decision": "allow",
                    "created_at": datetime.now(UTC).isoformat(),
                    "invalid_field": "bad",
                }
            ],
        }
        self.store._permission_file.write_text(json.dumps(data))
        self.store._initialized = False

        # Should skip invalid permission and continue
        permissions = self.store.list_permissions()
        assert len(permissions) == 1  # The permission should still load despite extra field

    def test_thread_safety(self) -> None:
        """Test that concurrent access is handled correctly."""
        errors = []

        def worker(worker_id: int) -> None:
            try:
                for i in range(100):
                    self.store.grant_permission(
                        path_pattern=f"/home/user/worker{worker_id}_file{i}.txt",
                        scope=FileAccessScope.HOME,
                        operations={"read"},
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(self.store._permissions) == 1000

    def test_request_permission_workspace_scope(self) -> None:
        result = self.store.request_permission(
            path="/workspace/file.txt",
            operation="read",
            scope=FileAccessScope.WORKSPACE,
        )
        assert result == FileAccessDecision.ALLOW

    def test_request_permission_home_scope_auto_allow(self) -> None:
        with patch("pathlib.Path.home", return_value=Path("/home/user")):
            result = self.store.request_permission(
                path="/home/user/document.txt",
                operation="read",
                scope=FileAccessScope.HOME,
            )
            assert result == FileAccessDecision.ALLOW

    def test_request_permission_home_scope_not_in_home(self) -> None:
        with patch("pathlib.Path.home", return_value=Path("/home/user")):
            result = self.store.request_permission(
                path="/etc/passwd",
                operation="read",
                scope=FileAccessScope.HOME,
            )
            assert result == FileAccessDecision.DENY

    def test_request_permission_system_scope(self) -> None:
        result = self.store.request_permission(
            path="/etc/passwd",
            operation="read",
            scope=FileAccessScope.SYSTEM,
        )
        assert result == FileAccessDecision.DENY

    def test_request_permission_external_scope(self) -> None:
        result = self.store.request_permission(
            path="/mnt/external/file.txt",
            operation="read",
            scope=FileAccessScope.EXTERNAL,
        )
        assert result == FileAccessDecision.DENY

    def test_request_permission_existing_allow(self) -> None:
        self.store.grant_permission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations={"read"},
            decision=FileAccessDecision.ALWAYS_ALLOW,
        )

        result = self.store.request_permission(
            path="/home/user/file.txt",
            operation="read",
            scope=FileAccessScope.HOME,
        )
        assert result == FileAccessDecision.ALLOW

    def test_request_permission_existing_deny(self) -> None:
        self.store.grant_permission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations={"read"},
            decision=FileAccessDecision.ALWAYS_DENY,
        )

        result = self.store.request_permission(
            path="/home/user/file.txt",
            operation="read",
            scope=FileAccessScope.HOME,
        )
        assert result == FileAccessDecision.DENY

    def test_atomic_write(self) -> None:
        """Test that writes are atomic (no partial writes)."""
        self.store.grant_permission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations={"read"},
        )

        # Verify no .tmp file remains
        tmp_files = list(Path(self.temp_dir).glob("*.tmp"))
        assert len(tmp_files) == 0

        # Verify file is valid JSON
        content = json.loads(self.store._permission_file.read_text())
        assert content["version"] == 1

    def test_permission_with_expiration(self) -> None:
        from datetime import timedelta

        expires_at = datetime.now(UTC) + timedelta(hours=1)
        permission = FileAccessPermission(
            path_pattern="/home/user/temp.txt",
            scope=FileAccessScope.HOME,
            operations=frozenset({"read"}),
            decision=FileAccessDecision.ALLOW,
            created_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        self.store._permissions[permission_key(permission)] = permission
        self.store._initialized = True

        self.store._save_to_disk()

        # Reload
        new_store = FilePermissionStore(Path(self.temp_dir))
        permissions = new_store.list_permissions()
        assert len(permissions) == 1
        assert permissions[0].expires_at is not None

    def test_permission_source_tracking(self) -> None:
        self.store.grant_permission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations={"read"},
            source="system",
        )

        permissions = self.store.list_permissions()
        assert len(permissions) == 1
        assert permissions[0].source == "system"

    def test_grant_permission_updates_existing(self) -> None:
        self.store.grant_permission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations={"read"},
            decision=FileAccessDecision.ALLOW,
        )

        # Grant again with different operations
        self.store.grant_permission(
            path_pattern="/home/user/file.txt",
            scope=FileAccessScope.HOME,
            operations={"read", "write"},
            decision=FileAccessDecision.ALWAYS_ALLOW,
        )

        # Should still be only one permission (updated)
        assert len(self.store._permissions) == 1
        permission = self.store._permissions["home:/home/user/file.txt"]
        assert permission.operations == frozenset({"read", "write"})
        assert permission.decision == FileAccessDecision.ALWAYS_ALLOW


class TestGetFilePermissionStore:
    """Tests for the global instance getter."""

    def test_returns_singleton(self) -> None:
        store1 = get_file_permission_store()
        store2 = get_file_permission_store()
        assert store1 is store2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
