"""Tests for atomic_write utilities."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from app.deepspace.utils.atomic_write import (
    FileLock,
    atomic_write_json,
    with_file_lock,
    with_file_lock_async,
    write_file_atomic,
    write_json_atomic,
)


class TestWriteFileAtomic:
    """Tests for write_file_atomic."""

    def test_write_string_atomic(self, tmp_path: Path) -> None:
        """Test atomic write of string content."""
        file_path = tmp_path / "test.txt"
        write_file_atomic(file_path, "hello world")
        assert file_path.read_text() == "hello world"

    def test_write_bytes_atomic(self, tmp_path: Path) -> None:
        """Test atomic write of bytes content."""
        file_path = tmp_path / "test.bin"
        write_file_atomic(file_path, b"binary data")
        assert file_path.read_bytes() == b"binary data"

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Test that parent directories are created."""
        file_path = tmp_path / "nested" / "deep" / "test.txt"
        write_file_atomic(file_path, "content")
        assert file_path.read_text() == "content"

    def test_write_replaces_existing(self, tmp_path: Path) -> None:
        """Test that atomic write replaces existing file."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("old")
        write_file_atomic(file_path, "new")
        assert file_path.read_text() == "new"

    def test_write_preserves_mode(self, tmp_path: Path) -> None:
        """Test that file mode is applied."""
        file_path = tmp_path / "test.txt"
        write_file_atomic(file_path, "content", mode=0o600)
        stat = file_path.stat()
        assert stat.st_mode & 0o777 == 0o600

    def test_write_cleans_temp_on_error(self, tmp_path: Path) -> None:
        """Test that temp file is cleaned up on error."""
        file_path = tmp_path / "test.txt"

        # Make parent read-only to cause write failure
        if os.name != "nt":
            tmp_path.chmod(0o555)
            try:
                with pytest.raises(OSError):
                    write_file_atomic(file_path, "content")
            finally:
                tmp_path.chmod(0o755)

            # No temp files should remain
            temp_files = list(tmp_path.glob("*.tmp"))
            assert len(temp_files) == 0


class TestWriteJsonAtomic:
    """Tests for write_json_atomic."""

    def test_write_json_atomic(self, tmp_path: Path) -> None:
        """Test atomic JSON write."""
        file_path = tmp_path / "test.json"
        payload = {"key": "value", "num": 42, "nested": {"a": 1}}
        write_json_atomic(file_path, payload)
        assert json.loads(file_path.read_text()) == payload

    def test_write_json_sorted_keys(self, tmp_path: Path) -> None:
        """Test that JSON keys are sorted."""
        file_path = tmp_path / "test.json"
        write_json_atomic(file_path, {"z": 1, "a": 2})
        content = file_path.read_text()
        assert content.index('"a"') < content.index('"z"')


class TestAtomicWriteJson:
    """Tests for atomic_write_json (enhanced with locking)."""

    def test_atomic_write_json_basic(self, tmp_path: Path) -> None:
        """Test basic atomic JSON write with locking."""
        file_path = tmp_path / "registry.json"
        atomic_write_json(file_path, {"version": 2, "data": [1, 2, 3]})
        assert json.loads(file_path.read_text()) == {"version": 2, "data": [1, 2, 3]}

    def test_concurrent_writes_serialized(self, tmp_path: Path) -> None:
        """Test that concurrent writes are serialized via file lock."""
        file_path = tmp_path / "concurrent.json"
        results: list[str] = []
        errors: list[Exception] = []

        def writer(value: str) -> None:
            try:
                atomic_write_json(file_path, {"value": value})
                results.append(value)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"val{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        # Final value should be one of the written values
        final = json.loads(file_path.read_text())
        assert final["value"] in results


class TestFileLock:
    """Tests for FileLock cross-process locking."""

    def test_lock_acquire_release(self, tmp_path: Path) -> None:
        """Test basic lock acquire/release."""
        lock_file = tmp_path / "test.lock"
        lock = FileLock(lock_file, wait_ms=1000)

        lock.acquire()
        assert lock._acquired
        assert lock_file.exists()

        lock.release()
        assert not lock._acquired
        assert not lock_file.exists()

    def test_lock_context_manager(self, tmp_path: Path) -> None:
        """Test lock as context manager."""
        lock_file = tmp_path / "test.lock"

        with FileLock(lock_file, wait_ms=1000) as lock:
            assert lock._acquired
            assert lock_file.exists()

        assert not lock_file.exists()

    def test_lock_blocks_other_process(self, tmp_path: Path) -> None:
        """Test that lock blocks other threads."""
        lock_file = tmp_path / "test.lock"
        acquired = threading.Event()
        released = threading.Event()

        def holder():
            with FileLock(lock_file, wait_ms=5000):
                acquired.set()
                released.wait(timeout=2)

        def waiter():
            acquired.wait(timeout=2)
            lock = FileLock(lock_file, wait_ms=1000)
            # Should timeout waiting
            with pytest.raises(TimeoutError):
                lock.acquire()

        t1 = threading.Thread(target=holder)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t2.join(timeout=3)
        released.set()
        t1.join(timeout=2)

    def test_lock_timeout(self, tmp_path: Path) -> None:
        """Test lock acquisition timeout."""
        lock_file = tmp_path / "test.lock"

        # Hold lock in this thread
        lock = FileLock(lock_file, wait_ms=1000)
        lock.acquire()

        # Try to acquire with short timeout
        lock2 = FileLock(lock_file, wait_ms=100)
        with pytest.raises(TimeoutError):
            lock2.acquire()

        lock.release()

    def test_stale_lock_takeover(self, tmp_path: Path) -> None:
        """Test that stale locks (dead PID) are taken over."""
        lock_file = tmp_path / "test.lock"

        # Manually create a lock file with a dead PID
        lock_file.write_text("999999\n")  # Very unlikely to be a real PID

        # Should be able to acquire (take over stale lock)
        lock = FileLock(lock_file, wait_ms=1000)
        lock.acquire()
        assert lock._acquired
        lock.release()


class TestWithFileLock:
    """Tests for with_file_lock convenience function."""

    def test_with_file_lock_basic(self, tmp_path: Path) -> None:
        """Test with_file_lock executes operation under lock."""
        file_path = tmp_path / "data.json"
        lock_path = tmp_path / "data.json.lock"

        result = with_file_lock(file_path, lambda: {"computed": 42})
        assert result == {"computed": 42}
        assert not lock_path.exists()  # Lock released

    def test_with_file_lock_exception(self, tmp_path: Path) -> None:
        """Test lock is released even on exception."""
        file_path = tmp_path / "data.json"
        lock_path = tmp_path / "data.json.lock"

        with pytest.raises(ValueError):
            with_file_lock(file_path, lambda: (_ for _ in ()).throw(ValueError("fail")))

        assert not lock_path.exists()


class TestAsyncFileLock:
    """Tests for async file lock operations."""

    @pytest.mark.asyncio
    async def test_async_lock_acquire_release(self, tmp_path: Path) -> None:
        """Test async lock acquire/release."""
        lock_file = tmp_path / "test.lock"
        lock = FileLock(lock_file, wait_ms=1000)

        await lock.acquire_async()
        assert lock._acquired
        assert lock_file.exists()

        await lock.release_async()
        assert not lock._acquired
        assert not lock_file.exists()

    @pytest.mark.asyncio
    async def test_async_context_manager(self, tmp_path: Path) -> None:
        """Test async lock as context manager."""
        lock_file = tmp_path / "test.lock"

        async with FileLock(lock_file, wait_ms=1000) as lock:
            assert lock._acquired
            assert lock_file.exists()

        assert not lock_file.exists()

    @pytest.mark.asyncio
    async def test_with_file_lock_async(self, tmp_path: Path) -> None:
        """Test async with_file_lock."""
        file_path = tmp_path / "data.json"

        async def compute():
            await asyncio.sleep(0.01)
            return {"async": True}

        result = await with_file_lock_async(file_path, compute)
        assert result == {"async": True}


class TestBackwardCompatibility:
    """Tests that the enhanced atomic_write_json is a drop-in replacement."""

    def test_same_signature(self, tmp_path: Path) -> None:
        """Test that atomic_write_json has same signature as before."""
        file_path = tmp_path / "test.json"
        atomic_write_json(file_path, {"key": "value"})
        assert json.loads(file_path.read_text()) == {"key": "value"}

    def test_raises_oserror_on_failure(self, tmp_path: Path) -> None:
        """Test that OSError is raised on write failure."""
        if os.name == "nt":
            pytest.skip("Windows permissions differ")

        file_path = tmp_path / "readonly" / "test.json"
        file_path.parent.mkdir()
        file_path.parent.chmod(0o555)

        try:
            with pytest.raises(OSError):
                atomic_write_json(file_path, {"key": "value"})
        finally:
            file_path.parent.chmod(0o755)
