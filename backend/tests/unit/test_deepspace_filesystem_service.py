"""Tests for the DeepSpace filesystem service."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.deepspace.services.filesystem import (
    DeepSpaceFileSystemService,
    FileSystemRefusal,
)


class TestDeepSpaceFileSystemService:
    """Tests for the unified filesystem service."""

    @pytest.fixture
    def temp_workspace(self) -> Path:
        with tempfile.TemporaryDirectory() as tmp:
            yield Path(tmp)

    @pytest.fixture
    def fs_service(self, temp_workspace: Path) -> DeepSpaceFileSystemService:
        return DeepSpaceFileSystemService(
            workspace_root=temp_workspace,
            sandbox_mode="workspace-write",
        )

    def test_read_write_file(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test basic read/write cycle."""
        # Write a file
        result = asyncio.run(fs_service.write("test.txt", "Hello, World!", mode="create"))
        assert result.path == "test.txt"
        assert result.mode == "create"
        assert result.created is True
        assert len(result.version) == 64  # SHA-256 hex

        # Read it back
        read_result = asyncio.run(fs_service.read("test.txt"))
        assert "Hello, World!" in read_result.content
        assert read_result.line_count == 1
        assert read_result.version == result.version

    def test_write_overwrite(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test overwrite mode."""
        asyncio.run(fs_service.write("test.txt", "First", mode="create"))
        result = asyncio.run(fs_service.write("test.txt", "Second", mode="overwrite"))
        assert result.mode == "overwrite"
        assert result.created is False

        read_result = asyncio.run(fs_service.read("test.txt"))
        assert "Second" in read_result.content

    def test_write_append(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test append mode."""
        asyncio.run(fs_service.write("test.txt", "First\n", mode="create"))
        result = asyncio.run(fs_service.write("test.txt", "Appended\n", mode="append"))
        assert result.mode == "append"

        read_result = asyncio.run(fs_service.read("test.txt"))
        assert "First" in read_result.content
        assert "Appended" in read_result.content

    def test_edit_replace(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test edit replace operation."""
        asyncio.run(fs_service.write("test.txt", "Original content", mode="create"))
        version = asyncio.run(fs_service.read("test.txt")).version

        result = asyncio.run(
            fs_service.edit(
                "test.txt",
                "replace",
                content="New content",
                expected_version=version,
            )
        )
        assert result.operation == "replace"
        assert "New content" in asyncio.run(fs_service.read("test.txt")).content

    def test_edit_version_check(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test edit refuses stale version."""
        asyncio.run(fs_service.write("test.txt", "Original", mode="create"))
        version = asyncio.run(fs_service.read("test.txt")).version

        # Get the absolute path to modify externally
        stat = fs_service.stat("test.txt")
        Path(stat.absolute_path).write_text("External change")

        # Now try to edit with old version - should fail
        with pytest.raises(FileSystemRefusal) as exc:
            asyncio.run(
                fs_service.edit(
                    "test.txt",
                    "replace",
                    content="New",
                    expected_version=version,
                )
            )
        assert exc.value.code == "FS_STALE_VERSION"

    def test_str_replace(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test string replacement."""
        asyncio.run(fs_service.write("test.txt", "Hello world", mode="create"))
        version = asyncio.run(fs_service.read("test.txt")).version

        result = asyncio.run(
            fs_service.str_replace(
                "test.txt",
                "world",
                "DeepSpace",
                expected_version=version,
            )
        )
        assert result.replaced == 1

        read_result = asyncio.run(fs_service.read("test.txt"))
        assert "DeepSpace" in read_result.content
        assert "world" not in read_result.content

    def test_str_replace_not_found(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test str_replace fails when old_string not found."""
        asyncio.run(fs_service.write("test.txt", "Hello", mode="create"))
        version = asyncio.run(fs_service.read("test.txt")).version

        with pytest.raises(FileSystemRefusal) as exc:
            asyncio.run(
                fs_service.str_replace(
                    "test.txt",
                    "nonexistent",
                    "new",
                    expected_version=version,
                )
            )
        assert exc.value.code == "NOT_FOUND"

    def test_str_replace_ambiguous(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test str_replace fails when multiple matches without replace_all."""
        asyncio.run(fs_service.write("test.txt", "foo bar foo", mode="create"))
        version = asyncio.run(fs_service.read("test.txt")).version

        with pytest.raises(FileSystemRefusal) as exc:
            asyncio.run(
                fs_service.str_replace(
                    "test.txt",
                    "foo",
                    "baz",
                    expected_version=version,
                    replace_all=False,
                )
            )
        assert exc.value.code == "AMBIGUOUS"

    def test_str_replace_all(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test str_replace with replace_all."""
        asyncio.run(fs_service.write("test.txt", "foo bar foo", mode="create"))
        version = asyncio.run(fs_service.read("test.txt")).version

        result = asyncio.run(
            fs_service.str_replace(
                "test.txt",
                "foo",
                "baz",
                expected_version=version,
                replace_all=True,
            )
        )
        assert result.replaced == 2

        read_result = asyncio.run(fs_service.read("test.txt"))
        assert read_result.content.strip() == "1\tbaz bar baz"

    def test_delete_file(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test file deletion."""
        asyncio.run(fs_service.write("test.txt", "Content", mode="create"))
        result = asyncio.run(fs_service.delete("test.txt"))
        assert result["status"] == "ok"
        assert result["deleted"] == "file"

        with pytest.raises(FileSystemRefusal) as exc:
            asyncio.run(fs_service.read("test.txt"))
        assert exc.value.code == "NOT_FOUND"

    def test_delete_directory_recursive(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test directory deletion with recursive."""
        asyncio.run(fs_service.write("dir/file1.txt", "a", mode="create"))
        asyncio.run(fs_service.write("dir/file2.txt", "b", mode="create"))
        result = asyncio.run(fs_service.delete("dir", recursive=True))
        assert result["status"] == "ok"
        assert result["deleted"] == "directory"

    def test_version(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test version retrieval."""
        asyncio.run(fs_service.write("test.txt", "Content", mode="create"))
        version = fs_service.version("test.txt")
        assert len(version) == 64  # SHA-256 hex

    def test_stat(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test file stat."""
        asyncio.run(fs_service.write("test.txt", "Content", mode="create"))
        stat = fs_service.stat("test.txt")
        assert stat.path == "test.txt"
        assert stat.is_file
        assert stat.size_bytes > 0
        assert stat.version is not None

    def test_list_directory(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test directory listing."""
        asyncio.run(fs_service.write("file1.txt", "a", mode="create"))
        asyncio.run(fs_service.write("file2.txt", "b", mode="create"))
        asyncio.run(fs_service.write("sub/file3.txt", "c", mode="create"))

        files = fs_service.list(".", recursive=True)
        paths = {f.path for f in files}
        assert "file1.txt" in paths
        assert "file2.txt" in paths
        assert "sub/file3.txt" in paths

    def test_glob(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test glob pattern matching."""
        asyncio.run(fs_service.write("test_1.txt", "a", mode="create"))
        asyncio.run(fs_service.write("test_2.txt", "b", mode="create"))
        asyncio.run(fs_service.write("other.txt", "c", mode="create"))

        results = fs_service.glob("test_*.txt")
        paths = {r.path for r in results}
        assert "test_1.txt" in paths
        assert "test_2.txt" in paths
        assert "other.txt" not in paths

    def test_grep(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test text search."""
        asyncio.run(fs_service.write("file1.txt", "hello world", mode="create"))
        asyncio.run(fs_service.write("file2.txt", "goodbye world", mode="create"))

        results = fs_service.grep("world")
        assert len(results) == 2
        paths = {r["path"] for r in results}
        assert "file1.txt" in paths
        assert "file2.txt" in paths

    def test_lock_unlock(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test file locking."""
        asyncio.run(fs_service.write("test.txt", "Content", mode="create"))

        asyncio.run(fs_service.lock("test.txt"))
        # Should be able to unlock
        fs_service.unlock("test.txt")

    def test_watch(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test file watching."""
        asyncio.run(fs_service.write("test.txt", "Content", mode="create"))

        events = []

        def callback(path: str) -> None:
            events.append(path)

        unwatch = fs_service.watch("test.txt", callback)

        # Modify file
        asyncio.run(fs_service.write("test.txt", "New content", mode="overwrite"))

        # Give watcher time to fire
        import time

        time.sleep(0.1)

        assert "test.txt" in events

        unwatch()
        events.clear()
        asyncio.run(fs_service.write("test.txt", "Again", mode="overwrite"))
        time.sleep(0.1)
        assert "test.txt" not in events

    def test_sandbox_mode_enforcement(self, temp_workspace) -> None:
        """Test sandbox mode prevents escaping workspace."""
        service = DeepSpaceFileSystemService(
            workspace_root=temp_workspace,
            sandbox_mode="workspace-write",
        )

        # Try to access parent directory
        with pytest.raises(FileSystemRefusal) as exc:
            asyncio.run(service.read("../etc/passwd"))
        assert exc.value.code in ("SANDBOX_MODE_REQUIRED", "WORKSPACE_PATH_ERROR")

    def test_atomic_write_durability(self, fs_service: DeepSpaceFileSystemService) -> None:
        """Test that writes are atomic (no partial reads)."""
        # This is hard to test reliably, but we can verify the file exists
        # and is complete after write
        asyncio.run(fs_service.write("test.txt", "X" * 10000, mode="create"))

        read_result = asyncio.run(fs_service.read("test.txt"))
        assert len(read_result.content) > 0
        assert "X" in read_result.content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
