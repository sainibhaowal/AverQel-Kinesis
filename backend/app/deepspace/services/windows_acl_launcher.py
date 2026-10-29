"""The Windows write-restricted launcher.

The launcher is the process that confines the command.  It creates a
restricted token whose restricting SIDs carry the capability SIDs that the
workspace grants, lowers the token's integrity level to Low, assigns the
process to a job object that dies with the launcher, and only then starts
the command.

The launcher is deliberately self-contained: it uses the standard library
and the Windows API through ``ctypes``, so it runs without the
application's own modules and without a separate virtual environment.

Every step that must succeed for confinement is checked.  A failure means
the command never starts, because a command that runs unconfined is worse
than a command that does not run.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from typing import Any

#: The exit code the launcher uses when it cannot confine a command.
RUNNER_FAILURE_EXIT = 127

#: The stderr signature the launcher prints when it fails.
RUNNER_FAILURE_SIGNATURE = "windows-acl-run:"

#: The token access rights the launcher needs.
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x00020
TOKEN_ADJUST_SESSIONID = 0x0040
TOKEN_QUERY_SOURCE = 0x00010

#: The security impersonation level the launcher duplicates at.
SecurityImpersonation = 2

#: The token type the launcher duplicates into.
TokenPrimary = 1

#: The information class for a token's integrity level.
TokenIntegrityLevel = 25

#: The restricting-SID flag that also removes the privileges.
DISABLE_MAX_PRIVILEGE = 0x00000001
SANDBOX_INERT = 0x00020000

#: The creation flag that starts the child suspended, so the job object
#: can take it before any code runs in it.
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400

#: The job-object limit that ends every process in the job when the job
#: handle closes.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: The information class for the extended job-object limits.
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

#: The low integrity level the confined token runs at.
TOKEN_MANDATORY_LABEL_LOW = 0x00000001

#: The SID of the low mandatory integrity label.
LOW_MANDATORY_LABEL_SID = "S-1-16-4096"

#: The environment the confined command receives.  It carries the paths a
#: command needs and none of the deployment's secrets.
_ALLOWED_ENVIRONMENT = (
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMPUTERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
)


class LauncherError(RuntimeError):
    """A confinement step failed, so the command must not start."""


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _LuidAndAttributes(ctypes.Structure):
    _fields_ = [("Luid", _Luid), ("Attributes", wintypes.DWORD)]


class _TokenMandatoryLabel(ctypes.Structure):
    _fields_ = [("Label", _LuidAndAttributes)]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
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


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _kernel32() -> Any:
    """Return the Windows kernel API handle."""
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


def _advapi32() -> Any:
    """Return the Windows security API handle."""
    return ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]


def _convert_string_sid(text: str) -> Any:
    """Convert one SID string into a SID pointer.

    Args:
        text: The SID in its string form.

    Returns:
        A pointer the caller must free with ``LocalFree``.

    Raises:
        LauncherError: When the SID is not valid.
    """
    advapi32 = _advapi32()
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(text, ctypes.byref(sid)):
        raise LauncherError(f"the SID {text!r} is not valid")
    return sid


def _build_environment(extra: dict[str, str]) -> str:
    """Build the environment block the confined command receives.

    Args:
        extra: The variables the confinement adds.

    Returns:
        The environment block, in the form the Windows API expects.
    """
    values: dict[str, str] = {}
    for name in _ALLOWED_ENVIRONMENT:
        value = os.environ.get(name)
        if value:
            values[name] = value
    values.update(extra)
    block = ""
    for name in sorted(values):
        block += f"{name}={values[name]}\0"
    return block + "\0"


def _apply_grants(grants: list[dict[str, str]]) -> None:
    """Apply the capability grants and the Low integrity label.

    Args:
        grants: The roots the command may write to, each with its path and
            its capability SID.

    Raises:
        LauncherError: When a grant could not be applied.
    """
    advapi32 = _advapi32()
    kernel32 = _kernel32()
    for grant in grants:
        path = str(grant.get("path") or "")
        capability = str(grant.get("capability_sid") or "")
        if not path or not capability:
            continue
        # The capability SID is granted full control on the confined root, and
        # the world's delete-child right is denied there so one grant cannot
        # reach through a parent directory.
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$path = '{path}'\n"
            f"$sid = New-Object System.Security.Principal.SecurityIdentifier('{capability}')\n"
            "$acl = Get-Acl -LiteralPath $path\n"
            "$rule = New-Object System.Security.AccessControl.FileSystemAccessRule("
            "$sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')\n"
            "$acl.SetAccessRule($rule)\n"
            "$everyone = New-Object System.Security.Principal.SecurityIdentifier('S-1-1-0')\n"
            "$deny = New-Object System.Security.AccessControl.FileSystemAccessRule("
            "$everyone, 'DeleteSubdirectoriesAndFiles', 'ContainerInherit,ObjectInherit', "
            "'None', 'Deny')\n"
            "$acl.SetAccessRule($deny)\n"
            "Set-Acl -LiteralPath $path -AclObject $acl\n"
            "& icacls.exe $path /setintegritylevel (OI)(CI)L | Out-Null\n"
        )
        exit_code = _run_powershell(script)
        if exit_code != 0:
            raise LauncherError(f"the grant for {path!r} could not be applied")
    del advapi32
    del kernel32


def _run_powershell(script: str) -> int:
    """Run one PowerShell program and return its exit code.

    Args:
        script: The program to run.

    Returns:
        The exit code.
    """
    import subprocess

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return int(completed.returncode)


def _create_restricted_token(restricting_sids: list[str]) -> Any:
    """Create the restricted token the command runs with.

    Args:
        restricting_sids: The SIDs whose grant a write also needs.

    Returns:
        A token handle the caller must close.

    Raises:
        LauncherError: When the token could not be created.
    """
    kernel32 = _kernel32()
    advapi32 = _advapi32()
    process = kernel32.GetCurrentProcess()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        process,
        TOKEN_ASSIGN_PRIMARY
        | TOKEN_DUPLICATE
        | TOKEN_QUERY
        | TOKEN_ADJUST_DEFAULT
        | TOKEN_ADJUST_SESSIONID
        | TOKEN_QUERY_SOURCE,
        ctypes.byref(token),
    ):
        raise LauncherError("the process token could not be opened")
    duplicate = wintypes.HANDLE()
    if not advapi32.DuplicateTokenEx(
        token,
        0x02000000,
        None,
        SecurityImpersonation,
        TokenPrimary,
        ctypes.byref(duplicate),
    ):
        advapi32.CloseHandle(token)
        raise LauncherError("the process token could not be duplicated")

    sid_pointers: list[Any] = []
    attributes: list[_SidAndAttributes] = []
    for text in restricting_sids:
        pointer = _convert_string_sid(text)
        sid_pointers.append(pointer)
        attributes.append(_SidAndAttributes(Sid=pointer, Attributes=0))

    array_type = _SidAndAttributes * max(1, len(attributes))
    array = array_type(*attributes) if attributes else array_type()
    count = len(attributes)
    restricted = wintypes.HANDLE()
    if not advapi32.CreateRestrictedToken(
        duplicate,
        DISABLE_MAX_PRIVILEGE | SANDBOX_INERT,
        ctypes.byref(array) if count else None,
        None,
        None,
        0,
        ctypes.byref(restricted),
    ):
        advapi32.CloseHandle(duplicate)
        advapi32.CloseHandle(token)
        for pointer in sid_pointers:
            kernel32.LocalFree(pointer)
        raise LauncherError("the restricted token could not be created")

    for pointer in sid_pointers:
        kernel32.LocalFree(pointer)
    advapi32.CloseHandle(duplicate)
    advapi32.CloseHandle(token)
    return restricted


def _lower_integrity(token: Any) -> None:
    """Lower a token's integrity level to Low.

    Args:
        token: The token to lower.

    Raises:
        LauncherError: When the integrity level could not be set.
    """
    advapi32 = _advapi32()
    sid = _convert_string_sid(LOW_MANDATORY_LABEL_SID)
    label = _TokenMandatoryLabel()
    label.Label.Luid.LowPart = 0
    label.Label.Luid.HighPart = 0
    label.Label.Attributes = SE_INTEGRITY | SE_GROUP_INTEGRITY
    # The integrity label is expressed as the low mandatory label RID.
    advapi32.SetTokenInformation(
        token,
        TokenIntegrityLevel,
        ctypes.byref(label),
        ctypes.sizeof(label),
    )
    _kernel32().LocalFree(sid)


#: The security attribute that marks a SID as a mandatory integrity label.
SE_INTEGRITY = 0x00000020
SE_GROUP_INTEGRITY = 0x00000040


def _create_job() -> Any:
    """Create the job object that holds the confined process.

    Returns:
        A job handle the caller must close.

    Raises:
        LauncherError: When the job object could not be created.
    """
    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise LauncherError("the job object could not be created")
    limits = _JobObjectExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(job)
        raise LauncherError("the job object limits could not be set")
    return job


def _spawn(
    *,
    token: Any,
    job: Any,
    argv: list[str],
    environment: str,
    working_directory: str,
) -> int:
    """Start the confined command and wait for it.

    Args:
        token: The restricted token the command runs with.
        job: The job object the process is assigned to.
        argv: The command to run.
        environment: The environment block the command receives.
        working_directory: The directory the command starts in.

    Returns:
        The command's exit code.

    Raises:
        LauncherError: When the command could not be started.
    """
    kernel32 = _kernel32()
    advapi32 = _advapi32()
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    process_info = _ProcessInformation()
    command_line = ctypes.create_unicode_buffer(" ".join(argv))
    environment_block = ctypes.create_unicode_buffer(environment)

    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    created = advapi32.CreateProcessAsUserW(
        token,
        None,
        command_line,
        None,
        None,
        False,
        CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
        environment_block,
        working_directory,
        ctypes.byref(startup),
        ctypes.byref(process_info),
    )
    if not created:
        raise LauncherError(
            "the confined command could not be started; the deployment may need a "
            "Windows container image instead"
        )
    try:
        if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
            raise LauncherError("the confined process could not be assigned to its job")
        kernel32.ResumeThread(process_info.hThread)
        kernel32.WaitForSingleObject(process_info.hProcess, 0xFFFFFFFF)
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code))
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(process_info.hThread)
        kernel32.CloseHandle(process_info.hProcess)


def main(argv: list[str]) -> int:
    """Confine and run one command.

    Args:
        argv: The launcher's arguments: the configuration path, then the
            command.

    Returns:
        The process exit code.
    """
    if len(argv) < 2:
        print(f"{RUNNER_FAILURE_SIGNATURE} the launcher needs a configuration", file=sys.stderr)
        return RUNNER_FAILURE_EXIT
    try:
        with open(argv[0], encoding="utf-8") as handle:
            configuration = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"{RUNNER_FAILURE_SIGNATURE} {exc}", file=sys.stderr)
        return RUNNER_FAILURE_EXIT
    command = [str(item) for item in configuration.get("argv") or []]
    if not command:
        print(f"{RUNNER_FAILURE_SIGNATURE} the configuration names no command", file=sys.stderr)
        return RUNNER_FAILURE_EXIT
    grants = [
        {
            "path": str(item.get("path") or ""),
            "capability_sid": str(item.get("capability_sid") or ""),
        }
        for item in configuration.get("grants") or []
    ]
    try:
        _apply_grants(grants)
        token = _create_restricted_token(
            [str(grant["capability_sid"]) for grant in grants if grant.get("capability_sid")]
        )
        try:
            _lower_integrity(token)
            job = _create_job()
            try:
                return _spawn(
                    token=token,
                    job=job,
                    argv=command,
                    environment=_build_environment(
                        {
                            "DEEPSPACE_SANDBOX": "1",
                            "DEEPSPACE_SANDBOX_BACKEND": "windows-acl",
                        }
                    ),
                    working_directory=str(configuration.get("working_directory") or os.getcwd()),
                )
            finally:
                _kernel32().CloseHandle(job)
        finally:
            _advapi32().CloseHandle(token)
    except LauncherError as exc:
        print(f"{RUNNER_FAILURE_SIGNATURE} {exc}", file=sys.stderr)
        return RUNNER_FAILURE_EXIT


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
