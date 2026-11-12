"""Native OS process containment for DeepSpace.

Current support:
- Linux: systemd-run --scope --user + probe/cache selector (LinuxScopeSelector)
- Windows: suspended process start + kill-on-close Win32 Job Objects
- Host-exit finalization: synchronous cleanup of all live ranges

This module provides managed process ranges that own entire descendant trees,
with fallback to the existing sandbox wrappers when native containment is
unavailable. A Windows child is never spawned by this module unless it can be
assigned to a Job Object before it runs.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import platform
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)
_CREATE_SUSPENDED = 0x00000004

# All live managed ranges for host-exit finalization
_LIVE_RANGES: list[ManagedRange] = []


@dataclass
class ManagedRange:
    """A managed process range owning an entire descendant tree."""

    pid: int
    platform: str
    created_at: float = field(default_factory=time.time)
    stopped: bool = False
    _cleanup: Callable[[], None] | None = None
    _signal: Callable[[str], None] | None = None

    def signal(self, sig: str) -> None:
        if self.stopped:
            return
        if self._signal is not None:
            try:
                self._signal(sig)
            except OSError:
                pass
            return
        try:
            if self.platform in {"linux", "posix"}:
                os.killpg(self.pid, _signal_number(sig))
            else:
                os.kill(self.pid, _signal_number(sig))
        except (ProcessLookupError, OSError):
            pass

    def terminate_for_host_exit(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self._signal is not None:
            try:
                self._signal("KILL")
            except OSError:
                pass
        else:
            try:
                if self.platform in {"linux", "posix"}:
                    os.killpg(self.pid, 9)  # SIGKILL
                else:
                    os.kill(self.pid, 9)
            except (ProcessLookupError, OSError):
                pass
        cleanup = self._cleanup
        self._cleanup = None
        if cleanup is not None:
            cleanup()

    def mark_stopped(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        cleanup = self._cleanup
        self._cleanup = None
        if cleanup is not None:
            cleanup()


def _signal_number(name: str) -> int:
    import signal as sig_mod

    name = name.lstrip("SIG")
    return getattr(sig_mod, f"SIG{name}", 9)


def process_group_spawn_options() -> dict[str, Any]:
    """Start POSIX children in their own process group for managed cleanup.

    This is lifecycle containment, not a resource or filesystem sandbox.
    Windows callers use the shared launcher, which starts children suspended
    and assigns a kill-on-close Job before resuming them.
    """
    return {"start_new_session": True} if os.name == "posix" else {}


def track_started_process(process: Any) -> ManagedRange | None:
    """Register a just-started process group for host-exit cleanup."""
    if os.name != "posix":
        return None
    try:
        return get_containment_manager().track_process_group(int(process.pid))
    except (AttributeError, TypeError, ValueError, OSError):
        logger.debug("Could not track child process group", exc_info=True)
        return None


def terminate_process_tree(process: Any, *, force: bool = True) -> None:
    """Stop a child started with :func:`process_group_spawn_options`.

    POSIX launches signal the complete process group. Other platforms fall
    back to the subprocess handle; they are not reported as Job-contained.
    """
    if os.name == "posix":
        try:
            os.killpg(
                int(process.pid),
                signal.SIGKILL if force else signal.SIGTERM,
            )
            return
        except (ProcessLookupError, PermissionError, OSError, TypeError, ValueError):
            pass
    try:
        (process.kill if force else process.terminate)()
    except (ProcessLookupError, OSError, AttributeError):
        pass


class LinuxScopeSelector:
    """Probe and cache Linux systemd-run scope availability.

    Re-runs the probe only after invalidation; warns on fallback.
    """

    def __init__(self) -> None:
        self._available: bool | None = None
        self._last_probe: float = 0.0
        self._probe_interval: float = 60.0  # seconds

    def probe(self) -> bool:
        now = time.time()
        if self._available is not None and (now - self._last_probe) < self._probe_interval:
            return self._available
        self._last_probe = now
        try:
            # Check if systemd-run is available
            result = subprocess.run(
                ["systemd-run", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self._available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            self._available = False
        if not self._available:
            logger.warning(
                "Linux native containment (systemd-run) unavailable; "
                "falling back to sandbox wrappers (weaker containment)"
            )
        return self._available

    def invalidate(self) -> None:
        self._available = None

    @property
    def available(self) -> bool:
        return self.probe()


class LinuxNativeRuntime:
    """Linux native containment using systemd-run --scope --user.

    Creates a private systemd user scope that owns the entire descendant tree.
    """

    def __init__(self) -> None:
        self._selector = LinuxScopeSelector()
        self._scopes: list[ManagedRange] = []

    def available(self) -> bool:
        return self._selector.available

    async def create_scope(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ManagedRange | None:
        """Create a systemd-run scope owning the process tree."""
        if not self._selector.available:
            return None
        try:
            cmd = ["systemd-run", "--scope", "--user", "--quiet", "--collect"]
            if cwd:
                cmd += ["--working-directory", cwd]
            cmd += ["--"]
            cmd += argv
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            range_obj = ManagedRange(pid=proc.pid, platform="linux")
            self._scopes.append(range_obj)
            _LIVE_RANGES.append(range_obj)
            return range_obj
        except Exception as exc:
            logger.warning("Failed to create systemd scope: %s", exc)
            self._selector.invalidate()
            return None

    async def create_private_bootstrap(
        self, argv: list[str], cwd: str, env: dict[str, str] | None = None
    ) -> ManagedRange | None:
        """Create a private bootstrap using unshare -U (user namespace)."""
        try:
            cmd = ["unshare", "-U", "--"]
            cmd += argv
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            range_obj = ManagedRange(pid=proc.pid, platform="linux")
            self._scopes.append(range_obj)
            _LIVE_RANGES.append(range_obj)
            return range_obj
        except Exception as exc:
            logger.debug("unshare bootstrap unavailable: %s", exc)
            return None

    def cleanup_scope(self, range_obj: ManagedRange) -> None:
        range_obj.mark_stopped()
        if range_obj in self._scopes:
            self._scopes.remove(range_obj)
        if range_obj in _LIVE_RANGES:
            _LIVE_RANGES.remove(range_obj)


class WindowsJobRuntime:
    """Windows native containment using Win32 Job API.

    Shared subprocess launches start suspended, enter a kill-on-close Job,
    then resume. A process is not reported as contained unless all steps work.
    """

    def __init__(self) -> None:
        self._controller: Any | None = None
        try:
            from app.deepspace.services.windows_job import WindowsJobController

            self._controller = WindowsJobController()
            probe = self._controller.create()
            probe.close()
        except Exception:  # noqa: BLE001 - unavailable containment must fail closed
            logger.warning("Windows Job Object containment is unavailable", exc_info=True)

    @property
    def available(self) -> bool:
        return self._controller is not None

    def _managed_range(self, process_id: int, job: Any) -> ManagedRange:
        def signal_job(_signal_name: str) -> None:
            # Job termination is tree-wide and does not have POSIX signal semantics.
            job.terminate(1)

        range_obj: ManagedRange

        def cleanup_job() -> None:
            job.close()
            if range_obj in _LIVE_RANGES:
                _LIVE_RANGES.remove(range_obj)

        range_obj = ManagedRange(
            pid=process_id,
            platform="windows",
            _cleanup=cleanup_job,
            _signal=signal_job,
        )
        _LIVE_RANGES.append(range_obj)
        return range_obj

    def assign_suspended_process(self, process: Any) -> ManagedRange:
        if self._controller is None:
            raise RuntimeError("Windows Job containment is unavailable")
        job = self._controller.create()
        try:
            self._controller.assign_and_resume(job, process)
        except Exception:
            job.close()
            raise
        return self._managed_range(int(process.pid), job)

    def assign_running_pid(self, process_id: int) -> ManagedRange:
        """Contain an already-running PTY child and its later descendants."""
        if self._controller is None:
            raise RuntimeError("Windows Job containment is unavailable")
        job = self._controller.create()
        try:
            self._controller.assign_pid(job, process_id)
        except Exception:
            job.close()
            raise
        return self._managed_range(process_id, job)

    async def create_job(
        self, argv: list[str], cwd: str, env: dict[str, str] | None = None
    ) -> ManagedRange | None:
        """Start a child suspended, assign its Job, resume, and reap it."""
        if self._controller is None:
            return None
        flags = int(getattr(subprocess, "CREATE_SUSPENDED", _CREATE_SUSPENDED)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=flags,
        )
        try:
            range_obj = self.assign_suspended_process(process)
        except Exception:
            process.kill()
            await process.wait()
            raise

        async def reap() -> None:
            await process.wait()
            range_obj.mark_stopped()
            if range_obj in _LIVE_RANGES:
                _LIVE_RANGES.remove(range_obj)

        asyncio.create_task(reap())
        return range_obj


class NativeContainmentManager:
    """Manages native containment across platforms."""

    def __init__(self) -> None:
        self.platform = platform.system().lower()
        self.linux = LinuxNativeRuntime() if self.platform == "linux" else None
        self.windows = WindowsJobRuntime() if self.platform == "windows" else None
        self._host_exit_registered = False

    def available(self) -> bool:
        if self.platform == "linux":
            return self.linux is not None and self.linux.available()
        if self.platform == "windows":
            return self.windows is not None and self.windows.available
        return False

    def track_process_group(self, pid: int) -> ManagedRange | None:
        """Track a POSIX process group created with ``start_new_session``.

        The range gives the host-exit handler a stable process-group handle
        and lets the caller release it when the child has been reaped. It is
        lifecycle containment only; it does not impose resource limits or a
        filesystem/network sandbox.
        """
        if os.name != "posix":
            return None
        try:
            if os.getpgid(pid) != pid:
                return None
        except (ProcessLookupError, PermissionError, OSError):
            return None

        range_obj: ManagedRange

        def remove_range() -> None:
            if range_obj in _LIVE_RANGES:
                _LIVE_RANGES.remove(range_obj)

        range_obj = ManagedRange(pid=pid, platform="posix", _cleanup=remove_range)
        _LIVE_RANGES.append(range_obj)
        return range_obj

    async def create_range(
        self, argv: list[str], cwd: str, env: dict[str, str] | None = None
    ) -> ManagedRange | None:
        if self.platform == "linux" and self.linux is not None:
            # Try systemd scope first
            result = await self.linux.create_scope(argv, cwd, env)
            if result is not None:
                return result
            # Fall back to private bootstrap
            return await self.linux.create_private_bootstrap(argv, cwd, env)
        if self.platform == "windows" and self.windows is not None:
            return await self.windows.create_job(argv, cwd, env)
        return None

    def assign_suspended_process(self, process: Any) -> ManagedRange:
        """Assign an asyncio child whose primary thread is still suspended."""
        if self.platform != "windows" or self.windows is None:
            raise RuntimeError("Windows Job containment is unavailable")
        return self.windows.assign_suspended_process(process)

    def assign_running_windows_pid(self, process_id: int) -> ManagedRange:
        """Assign a running Windows PTY child to a kill-on-close Job Object."""
        if self.platform != "windows" or self.windows is None:
            raise RuntimeError("Windows Job containment is unavailable")
        return self.windows.assign_running_pid(process_id)

    def register_host_exit(self) -> None:
        """Register atexit handler for host-exit finalization."""
        if self._host_exit_registered:
            return
        self._host_exit_registered = True
        atexit.register(self._finalize_all)

    def _finalize_all(self) -> None:
        """Synchronously terminate all live ranges (host-exit finalization)."""
        for range_obj in list(_LIVE_RANGES):
            try:
                range_obj.terminate_for_host_exit()
            except Exception:
                pass
        _LIVE_RANGES.clear()

    def live_ranges(self) -> list[ManagedRange]:
        return list(_LIVE_RANGES)


# Global instance
_containment_manager: NativeContainmentManager | None = None


def get_containment_manager() -> NativeContainmentManager:
    global _containment_manager
    if _containment_manager is None:
        _containment_manager = NativeContainmentManager()
        _containment_manager.register_host_exit()
    return _containment_manager
