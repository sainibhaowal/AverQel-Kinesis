"""Windows Job Object containment for a suspended process."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any


class WindowsJobError(RuntimeError):
    """A process could not be assigned to a Windows Job Object safely."""


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class WindowsJob:
    """A kill-on-close Job Object owning assigned child processes."""

    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self.handle = handle
        self.closed = False

    def assign(self, process_handle: Any) -> None:
        if self.closed:
            raise WindowsJobError("the Windows Job Object is already closed")
        if not self._kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise _last_error("the process could not be assigned to its Job Object")

    def terminate(self, exit_code: int = 1) -> None:
        if not self.closed:
            self._kernel32.TerminateJobObject(self.handle, exit_code)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._kernel32.CloseHandle(self.handle)


def _last_error(message: str) -> WindowsJobError:
    get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
    return WindowsJobError(f"{message} (Windows error {get_last_error()})")


class WindowsJobController:
    """Creates and assigns Job Objects before a suspended process can execute."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _THREAD_QUERY_INFORMATION = 0x0040
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, kernel32: Any | None = None) -> None:
        if kernel32 is None:
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            except (AttributeError, OSError) as exc:
                raise WindowsJobError("Windows Job Objects are available only on Windows") from exc
        self._kernel32 = kernel32

        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self._kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        self._kernel32.Thread32First.restype = wintypes.BOOL
        self._kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        self._kernel32.Thread32Next.restype = wintypes.BOOL
        self._kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenThread.restype = wintypes.HANDLE
        self._kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self._kernel32.ResumeThread.restype = wintypes.DWORD
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE

    def create(self) -> WindowsJob:
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _last_error("a Windows Job Object could not be created")
        job = WindowsJob(self._kernel32, handle)
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._kernel32.SetInformationJobObject(
            handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not ok:
            job.close()
            raise _last_error("the Job Object kill-on-close policy could not be set")
        return job

    def assign_and_resume(self, job: WindowsJob, process: Any) -> None:
        """Assign an asyncio child while its primary thread is still suspended."""
        transport = getattr(process, "_transport", None)
        popen = transport.get_extra_info("subprocess") if transport is not None else None
        raw_handle = getattr(popen, "_handle", None)
        process_handle = getattr(raw_handle, "handle", raw_handle)
        if process_handle is None:
            raise WindowsJobError("the suspended child process handle is unavailable")
        job.assign(process_handle)
        thread_handle = self._open_primary_thread(int(process.pid))
        try:
            previous_count = self._kernel32.ResumeThread(thread_handle)
            if previous_count == 0xFFFFFFFF or previous_count == 0:
                raise _last_error("the suspended child thread could not be resumed")
        finally:
            self._kernel32.CloseHandle(thread_handle)

    def assign_pid(self, job: WindowsJob, process_id: int) -> None:
        """Assign an already-running process to a Job Object."""
        process_handle = self._kernel32.OpenProcess(
            0x0001 | 0x0100 | 0x1000,  # TERMINATE | SET_QUOTA | QUERY_LIMITED_INFORMATION
            False,
            process_id,
        )
        if not process_handle:
            raise _last_error("the Windows PTY process could not be opened")
        try:
            job.assign(process_handle)
        finally:
            self._kernel32.CloseHandle(process_handle)

    def _open_primary_thread(self, process_id: int) -> Any:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(self._TH32CS_SNAPTHREAD, 0)
        if snapshot == self._INVALID_HANDLE_VALUE:
            raise _last_error("the suspended child thread could not be located")
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            found = self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process_id:
                    handle = self._kernel32.OpenThread(
                        self._THREAD_SUSPEND_RESUME | self._THREAD_QUERY_INFORMATION,
                        False,
                        entry.th32ThreadID,
                    )
                    if handle:
                        return handle
                found = self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        raise _last_error("the suspended child thread could not be opened")


__all__ = ["WindowsJob", "WindowsJobController", "WindowsJobError"]
