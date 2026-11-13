"""Atomic file write with cross-process locking for DeepSpace.

This module provides atomic file replacement with exclusive-create temp + rename,
and cross-process writer coordination via a `<file>.lock` sibling. It includes
PID-based stale-lock takeover, Windows transient-error retries, and fsync
durability — matching the semantics of @deepseek-ai/dsh-atomic-write.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


# Windows transient rename error codes
WINDOWS_TRANSIENT_RENAME_ERRORS: frozenset[str] = frozenset({"EACCES", "EBUSY", "EPERM"})
WINDOWS_RENAME_RETRY_INITIAL_MS = 20
WINDOWS_RENAME_RETRY_MAX_MS = 200
WINDOWS_RENAME_RETRY_LIMIT = 8

# Lock retry cadence
LOCK_RETRY_INITIAL_MS = 20
LOCK_RETRY_MAX_MS = 200
DEFAULT_LOCK_WAIT_MS = 2_000


def _is_transient_windows_rename_error(error: BaseException) -> bool:
    """Check if an error is a transient Windows rename interference."""
    if os.name != "nt":
        return False
    code = getattr(error, "errno", None) or getattr(error, "code", None)
    if code is None and hasattr(error, "winerror"):
        code = error.winerror
    # Map errno to error code names
    import errno

    code_name = errno.errorcode.get(code, "") if isinstance(code, int) else ""
    return code_name in WINDOWS_TRANSIENT_RENAME_ERRORS


async def _rename_atomic_temp(temp: str, filename: str) -> None:
    """Replace target after bounded retries for transient Windows interference."""
    delay = WINDOWS_RENAME_RETRY_INITIAL_MS
    for retries in range(WINDOWS_RENAME_RETRY_LIMIT + 1):
        try:
            os.replace(temp, filename)
            return
        except OSError as e:
            if not _is_transient_windows_rename_error(e):
                raise
            if retries >= WINDOWS_RENAME_RETRY_LIMIT:
                raise
        await _sleep_ms(delay)
        delay = min(delay * 2, WINDOWS_RENAME_RETRY_MAX_MS)


async def _sleep_ms(ms: int) -> None:
    """Sleep for milliseconds."""
    await asyncio.sleep(ms / 1000.0)


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry after a rename, where the platform supports it."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_lock_contention(error: BaseException, lock_path: Path) -> bool:
    """Check if an error indicates lock contention."""
    code = getattr(error, "errno", None) or getattr(error, "code", None)
    if code is None and hasattr(error, "winerror"):
        code = error.winerror
    import errno

    code_name = errno.errorcode.get(code, "") if isinstance(code, int) else ""
    if code_name == "EEXIST":
        return True
    if code_name != "EPERM":
        return False
    try:
        lock_path.stat()
        return True
    except OSError:
        return False


def _holder_exited(record: str) -> bool:
    """Check if the PID recorded in the lock file has exited."""
    if not record.endswith("\n"):
        return False
    try:
        pid = int(record.strip())
    except ValueError:
        return False
    if pid == 0 or pid > 0x7FFFFFFF:
        return False
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
        return False
    except OSError as e:
        return e.errno == 3  # ESRCH


async def _read_lock_record(lock_path: Path) -> str | None:
    """Read the lock file's content, or None if unreadable."""
    try:
        return lock_path.read_text(encoding="utf-8")
    except OSError:
        return None


async def _take_over_exited_lock(lock_path: Path) -> bool:
    """Remove the lock when its recorded holder exited.

    Uses a claim file to serialize contenders. Returns True if this call
    removed the dead holder's lock.
    """
    record = await _read_lock_record(lock_path)
    if record is None or not _holder_exited(record):
        return False

    claim_name = f".takeover-{hashlib.sha256(record.encode()).hexdigest()[:16]}"
    claim_path = lock_path.parent / (lock_path.name + claim_name)

    try:
        # Exclusive create the claim
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
    except OSError as e:
        if e.errno in (17, 13):  # EEXIST, EPERM
            return False
        raise

    try:
        # Re-verify the lock still has the same record and holder is gone
        if await _read_lock_record(lock_path) != record or not _holder_exited(record):
            return False
        try:
            lock_path.unlink()
        except OSError:
            return False
        return True
    finally:
        try:
            claim_path.unlink()
        except OSError:
            pass


def _read_lock_record_sync(lock_path: Path) -> str | None:
    """Synchronous version of _read_lock_record."""
    try:
        return lock_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _take_over_exited_lock_sync(lock_path: Path) -> bool:
    """Synchronous version of _take_over_exited_lock."""
    record = _read_lock_record_sync(lock_path)
    if record is None or not _holder_exited(record):
        return False

    claim_name = f".takeover-{hashlib.sha256(record.encode()).hexdigest()[:16]}"
    claim_path = lock_path.parent / (lock_path.name + claim_name)

    try:
        fd = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
    except OSError as e:
        if e.errno in (17, 13):  # EEXIST, EPERM
            return False
        raise

    try:
        if _read_lock_record_sync(lock_path) != record or not _holder_exited(record):
            return False
        try:
            lock_path.unlink()
        except OSError:
            return False
        return True
    finally:
        try:
            claim_path.unlink()
        except OSError:
            pass


class FileLock:
    """Cross-process writer lock for a file.

    The lock is a `O_CREAT | O_EXCL` created sibling (`<filename>.lock`).
    Paired with atomic rename-based commit, readers stay lock-free and
    only writers contend.
    """

    def __init__(
        self,
        filename: str | Path,
        *,
        wait_ms: int | None = DEFAULT_LOCK_WAIT_MS,
    ) -> None:
        self.filename = Path(filename)
        # An explicit ``*.lock`` path is useful to callers that manage lock
        # files directly; ordinary data paths get the conventional sibling.
        self.lock_path = (
            self.filename
            if self.filename.name.endswith(".lock")
            else self.filename.parent / (self.filename.name + ".lock")
        )
        self.wait_ms = wait_ms
        self._acquired = False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()

    async def __aenter__(self) -> FileLock:
        await self.acquire_async()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.release_async()

    def acquire(self) -> None:
        """Acquire the lock, blocking until available or timeout."""
        deadline = time.monotonic() + (self.wait_ms or 0) / 1000.0
        delay = LOCK_RETRY_INITIAL_MS
        retried_unconfirmed_perm = False

        while True:
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(fd)
                self._acquired = True
                return
            except OSError as e:
                if not _is_lock_contention(e, self.lock_path):
                    if os.name != "nt" or e.errno != 13 or retried_unconfirmed_perm:
                        raise
                    retried_unconfirmed_perm = True
                elif _take_over_exited_lock_sync(self.lock_path):
                    continue

            if self.wait_ms is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"atomic-write: timed out waiting for writer lock at {self.lock_path}"
                )

            time.sleep(delay / 1000.0)
            delay = min(delay * 2, LOCK_RETRY_MAX_MS)

    async def acquire_async(self) -> None:
        """Acquire without blocking the event loop."""
        import asyncio

        task = asyncio.create_task(asyncio.to_thread(self.acquire))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not strand a lock if cancellation races with acquisition.
            try:
                await task
            except Exception:
                pass
            if self._acquired:
                self.release()
            raise

    def release(self) -> None:
        """Release the lock."""
        if self._acquired:
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self._acquired = False

    async def release_async(self) -> None:
        """Async release the lock."""
        self.release()


def write_file_atomic(
    filename: str | Path,
    content: str | bytes,
    *,
    mode: int = 0o644,
    dir_mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """Replace `filename` with `content` in one atomic step.

    The content is first written to a random-suffix sibling opened with
    exclusive create (`O_EXCL`): the open refuses to follow a symlink planted
    at the temp path, and the fresh inode carries `mode` through the rename.
    The rename also replaces a symlinked target itself instead of writing
    through to its referent, and the same-directory sibling keeps the rename
    on one filesystem.

    Args:
        filename: Final path receiving the content.
        content: Complete next file content (str or bytes).
        mode: Permission bits for the replacement inode.
        dir_mode: Permission bits for parent directories created.
        encoding: Encoding for string content.

    Raises:
        OSError: On any failure (temp file is cleaned up).
    """
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode or 0o755)

    # Create temp file with random suffix
    import secrets

    temp_name = f".{filename.name}.{secrets.token_hex(6)}.tmp"
    temp_path = filename.parent / temp_name

    try:
        # Write content to temp file
        write_mode = "w" if isinstance(content, str) else "wb"
        with open(
            temp_path, write_mode, encoding=encoding if isinstance(content, str) else None
        ) as f:
            if isinstance(content, str):
                f.write(content)
            else:
                f.write(content)
            f.flush()
            os.fsync(f.fileno())

        # Set file mode
        temp_path.chmod(mode)

        # Atomic rename
        if os.name == "nt":
            # On Windows, use retry logic for transient errors
            import asyncio

            asyncio.run(_rename_atomic_temp(str(temp_path), str(filename)))
        else:
            os.replace(temp_path, filename)
            _fsync_directory(filename.parent)

    except BaseException:
        # Clean up temp file on any failure
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def write_json_atomic(
    filename: str | Path,
    payload: dict[str, Any],
    *,
    mode: int = 0o644,
    dir_mode: int | None = None,
    indent: int = 2,
    sort_keys: bool = True,
) -> None:
    """Atomically write a JSON document.

    Args:
        filename: Destination path.
        payload: Dictionary to serialize as JSON.
        mode: File permissions.
        dir_mode: Directory permissions.
        indent: JSON indentation.
        sort_keys: Sort dictionary keys.
    """
    import json

    content = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=sort_keys) + "\n"
    write_file_atomic(filename, content, mode=mode, dir_mode=dir_mode)


def with_file_lock(
    filename: str | Path,
    operation: Callable[[], T],
    *,
    wait_ms: int | None = DEFAULT_LOCK_WAIT_MS,
) -> T:
    """Hold the cross-process writer lock for `filename` around one operation.

    Args:
        filename: The file whose writers this lock serializes.
        operation: The read-render-commit cycle to run while holding the lock.
        wait_ms: Maximum time to wait for the lock (None = wait forever).

    Returns:
        The operation's result.

    Raises:
        TimeoutError: If the lock cannot be acquired within wait_ms.
    """
    lock = FileLock(filename, wait_ms=wait_ms)
    lock.acquire()
    try:
        return operation()
    finally:
        lock.release()


async def with_file_lock_async(
    filename: str | Path,
    operation: Callable[[], T | Awaitable[T]],
    *,
    wait_ms: int | None = DEFAULT_LOCK_WAIT_MS,
) -> T:
    """Async version of with_file_lock."""
    lock = FileLock(filename, wait_ms=wait_ms)
    await lock.acquire_async()
    try:
        result = operation()
        if inspect.isawaitable(result):
            return await result
        return result
    finally:
        await lock.release_async()


# Backward compatibility with existing workspace_registry.py
def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON document so readers see the old or the new file.

    This is the enhanced version with cross-process locking.

    Args:
        path: The destination.
        payload: The document to write.

    Raises:
        OSError: When the file could not be written or replaced.
    """
    # Use file lock for cross-process coordination
    with FileLock(path):
        write_json_atomic(path, payload)


__all__ = [
    "write_file_atomic",
    "write_json_atomic",
    "atomic_write_json",
    "with_file_lock",
    "with_file_lock_async",
    "FileLock",
    "DEFAULT_LOCK_WAIT_MS",
]
