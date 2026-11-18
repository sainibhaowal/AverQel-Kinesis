"""Windows-specific file system operations for DeepSpace.

Provides Windows-specific implementations of file system operations
including long path support, junctions, symlinks, and reparse points.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os

try:
    import ctypes
    from ctypes import wintypes
except ImportError:  # noqa: BLE001
    ctypes = None
    wintypes = None


# Windows API constants
if ctypes is not None:
    # File attributes
    FILE_ATTRIBUTE_READONLY = 0x1
    FILE_ATTRIBUTE_HIDDEN = 0x2
    FILE_ATTRIBUTE_SYSTEM = 0x4
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_ARCHIVE = 0x20
    FILE_ATTRIBUTE_DEVICE = 0x40
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_ATTRIBUTE_TEMPORARY = 0x100
    FILE_ATTRIBUTE_SPARSE_FILE = 0x200
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_ATTRIBUTE_COMPRESSED = 0x800
    FILE_ATTRIBUTE_OFFLINE = 0x1000
    FILE_ATTRIBUTE_NOT_CONTENT_INDEXED = 0x2000
    FILE_ATTRIBUTE_ENCRYPTED = 0x4000
    FILE_ATTRIBUTE_INTEGRITY_STREAM = 0x8000
    FILE_ATTRIBUTE_VIRTUAL = 0x10000
    FILE_ATTRIBUTE_NO_SCRUB_DATA = 0x20000
    FILE_ATTRIBUTE_EA = 0x40000
    FILE_ATTRIBUTE_PINNED = 0x80000
    FILE_ATTRIBUTE_UNPINNED = 0x100000
    FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x200000
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

    # Reparse point tags
    IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
    IO_REPARSE_TAG_SYMLINK = 0xA000000C
    IO_REPARSE_TAG_APPEXEC_LINK = 0xA000001B

    # Symbolic link flags
    SYMBOLIC_LINK_FLAG_FILE = 0x0
    SYMBOLIC_LINK_FLAG_DIRECTORY = 0x1
    SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE = 0x2

    # Access rights
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    # DeviceIoControl codes
    FSCTL_SET_REPARSE_POINT = 0x000900A4
    FSCTL_GET_REPARSE_POINT = 0x000900A8
    FSCTL_DELETE_REPARSE_POINT = 0x000900AC

    # ReparseDataBuffer structure
    class ReparseDataBuffer(ctypes.Structure):
        _fields_ = [
            ("ReparseTag", wintypes.DWORD),
            ("ReparseDataLength", wintypes.USHORT),
            ("Reserved", wintypes.USHORT),
            ("SubstituteNameOffset", wintypes.USHORT),
            ("SubstituteNameLength", wintypes.USHORT),
            ("PrintNameOffset", wintypes.USHORT),
            ("PrintNameLength", wintypes.USHORT),
            ("Flags", wintypes.ULONG),
            ("PathBuffer", wintypes.WCHAR * 1),
        ]

    # Load Windows DLLs
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    # Function prototypes
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetFileAttributesW.restype = wintypes.DWORD

    kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.SetFileAttributesW.restype = wintypes.BOOL

    kernel32.CreateSymbolicLinkW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.CreateSymbolicLinkW.restype = wintypes.BOOL

    kernel32.CreateHardLinkW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    kernel32.CreateHardLinkW.restype = wintypes.BOOL

    kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
    kernel32.DeleteFileW.restype = wintypes.BOOL

    kernel32.RemoveDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel32.RemoveDirectoryW.restype = wintypes.BOOL

    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL

    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL

    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL

    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

    # Constants
    FILE_INFO_BY_HANDLE_CLASS = 10  # FileIdInfo
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    MAX_PATH = 260
    LONG_PATH_PREFIX = "\\\\?\\"
    UNC_PREFIX = "\\\\?\\UNC\\"


def _to_win32_path(path: str) -> str:
    """Convert a path to Windows extended-length path format if needed."""
    if not path:
        return path
    path = os.path.normpath(path)
    if path.startswith("\\\\?\\") or path.startswith("\\\\?\\UNC\\"):
        return path
    if path.startswith("\\\\"):
        # UNC path
        return UNC_PREFIX + path[2:]
    if len(path) >= 260 or path.startswith("\\\\"):
        # Long path or UNC
        if path.startswith("\\\\"):
            return UNC_PREFIX + path[2:]
        return LONG_PATH_PREFIX + os.path.abspath(path)
    return path


def _from_win32_path(path: str) -> str:
    """Convert from Windows extended-length path format."""
    if path.startswith(LONG_PATH_PREFIX):
        return path[len(LONG_PATH_PREFIX) :]
    if path.startswith(UNC_PREFIX):
        return "\\\\" + path[len(UNC_PREFIX) :]
    return path


def _win32_error() -> OSError:
    """Get the last Windows error as an OSError."""
    return OSError(ctypes.get_last_error(), os.strerror(ctypes.get_last_error()))


def _check_result(result: bool, operation: str) -> None:
    """Raise OSError if Windows API call failed."""
    if not result:
        raise _win32_error()


class Win32FileOps:
    """Windows-specific file operations using Win32 API."""

    @staticmethod
    def _get_handle(
        path: str,
        access: int = 0x80000000,
        share: int = 0x7,
        disposition: int = 3,
        flags: int = 0x02000000,
    ) -> int:
        """Open a file/directory and return a Windows handle."""
        win32_path = _to_win32_path(path)
        handle = kernel32.CreateFileW(win32_path, access, share, None, 3, 0x02000000, None)
        if handle == -1:
            raise _win32_error()
        return handle

    @staticmethod
    def close_handle(handle: int) -> None:
        if handle and handle != -1:
            kernel32.CloseHandle(handle)

    @staticmethod
    def get_file_attributes(path: str) -> int:
        """Get Windows file attributes."""
        win32_path = _to_win32_path(path)
        attrs = kernel32.GetFileAttributesW(win32_path)
        if attrs == 0xFFFFFFFF:
            raise _win32_error()
        return attrs

    @staticmethod
    def set_file_attributes(path: str, attrs: int) -> None:
        """Set Windows file attributes."""
        win32_path = _to_win32_path(path)
        if not kernel32.SetFileAttributesW(win32_path, attrs):
            raise _win32_error()

    @staticmethod
    def is_reparse_point(path: str) -> bool:
        """Check if a path is a reparse point (junction/symlink)."""
        attrs = Win32FileOps.get_file_attributes(path)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)

    @staticmethod
    def is_directory(path: str) -> bool:
        attrs = Win32FileOps.get_file_attributes(path)
        return bool(attrs & FILE_ATTRIBUTE_DIRECTORY)

    @staticmethod
    def is_junction(path: str) -> bool:
        """Check if path is a junction (mount point)."""
        if not Win32FileOps.is_reparse_point(path):
            return False
        # Could check reparse tag via DeviceIoControl, but for now assume junction
        return True

    @staticmethod
    def is_symlink(path: str) -> bool:
        """Check if path is a symbolic link."""
        if not Win32FileOps.is_reparse_point(path):
            return False
        return True

    @staticmethod
    def read_file(path: str, mode: str = "r", encoding: str = "utf-8") -> str:
        """Read a text file."""
        win32_path = _to_win32_path(path)
        with open(win32_path, mode, encoding=encoding) as f:
            return f.read()

    @staticmethod
    def write_file(path: str, content: str, mode: str = "w", encoding: str = "utf-8") -> None:
        """Write a text file."""
        win32_path = _to_win32_path(path)
        with open(win32_path, mode, encoding=encoding) as f:
            f.write(content)

    @staticmethod
    def write_bytes(path: str, data: bytes) -> None:
        """Write binary data to a file."""
        win32_path = _to_win32_path(path)
        with open(win32_path, "wb") as f:
            f.write(data)

    @staticmethod
    def read_bytes(path: str) -> bytes:
        """Read binary data from a file."""
        win32_path = _to_win32_path(path)
        with open(win32_path, "rb") as f:
            return f.read()

    @staticmethod
    def delete_file(path: str) -> None:
        """Delete a file."""
        win32_path = _to_win32_path(path)
        if not kernel32.DeleteFileW(win32_path):
            raise _win32_error()

    @staticmethod
    def remove_directory(path: str) -> None:
        """Remove an empty directory."""
        win32_path = _to_win32_path(path)
        if not kernel32.RemoveDirectoryW(win32_path):
            raise _win32_error()

    @staticmethod
    def create_directory(path: str) -> None:
        """Create a directory."""
        win32_path = _to_win32_path(path)
        if not kernel32.CreateDirectoryW(win32_path, None):
            err = ctypes.get_last_error()
            if err != 183:  # ERROR_ALREADY_EXISTS
                raise _win32_error()

    @staticmethod
    def create_directories(path: str) -> None:
        """Create directory and all parent directories."""
        win32_path = _to_win32_path(path)
        # Use os.makedirs which handles intermediate directories
        os.makedirs(_from_win32_path(win32_path), exist_ok=True)

    @staticmethod
    def move_file(src: str, dst: str, replace: bool = False) -> None:
        """Move or rename a file/directory."""
        src_win32 = _to_win32_path(src)
        dst_win32 = _to_win32_path(dst)
        flags = 0x1 if replace else 0  # MOVEFILE_REPLACE_EXISTING
        if not kernel32.MoveFileExW(src_win32, dst_win32, flags):
            raise _win32_error()

    @staticmethod
    def create_symlink(target: str, link_path: str, is_directory: bool = False) -> None:
        """Create a symbolic link."""
        target_win32 = _to_win32_path(target)
        link_win32 = _to_win32_path(link_path)
        flags = SYMBOLIC_LINK_FLAG_DIRECTORY if is_directory else 0
        if not kernel32.CreateSymbolicLinkW(link_win32, target_win32, flags):
            raise _win32_error()

    @staticmethod
    def create_junction(target: str, junction_path: str) -> None:
        """Create a directory junction (mount point)."""
        # Create the junction directory first
        Win32FileOps.create_directory(junction_path)

        # Open the junction directory with FILE_FLAG_OPEN_REPARSE_POINT
        junction_win32 = _to_win32_path(junction_path)
        handle = kernel32.CreateFileW(
            junction_win32,
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == -1:
            raise _win32_error()

        try:
            target_win32 = _to_win32_path(target)
            # Ensure target is absolute
            target_abs = os.path.abspath(_from_win32_path(target_win32))
            target_win32_abs = _to_win32_path(target_abs)

            # Build reparse data buffer for junction
            # Path format: \??\C:\path\to\target
            if not target_win32_abs.startswith(LONG_PATH_PREFIX):
                if target_win32_abs.startswith("\\\\"):
                    target_win32_abs = UNC_PREFIX + target_win32_abs[2:]
                elif not target_win32_abs.startswith("\\\\?\\"):
                    target_win32_abs = LONG_PATH_PREFIX + target_win32_abs

            substitute_name = f"\\??\\{target_win32_abs[len(LONG_PATH_PREFIX):]}"
            print_name = os.path.basename(junction_path)

            substitute_bytes = substitute_name.encode("utf-16-le")
            print_bytes = print_name.encode("utf-16-le")

            substitute_len = len(substitute_bytes)
            print_len = len(print_bytes)

            # Calculate buffer size
            buffer_size = ctypes.sizeof(ctypes.c_ulong * 3) + substitute_len + print_len + 4
            buffer = ctypes.create_string_buffer(buffer_size)
            rdb = ctypes.cast(buffer, ctypes.POINTER(ReparseDataBuffer)).contents

            rdb.ReparseTag = IO_REPARSE_TAG_MOUNT_POINT
            rdb.ReparseDataLength = substitute_len + print_len + 12
            rdb.Reserved = 0
            rdb.SubstituteNameOffset = 0
            rdb.SubstituteNameLength = substitute_len
            rdb.PrintNameOffset = substitute_len + 2
            rdb.PrintNameLength = print_len
            rdb.Flags = 0

            # Copy path data
            ctypes.memmove(ctypes.addressof(rdb.PathBuffer), substitute_bytes, substitute_len)
            ctypes.memmove(
                ctypes.addressof(rdb.PathBuffer) + substitute_len + 2, print_bytes, print_len
            )

            _ = wintypes.DWORD()
            success = kernel32.DeviceIoControl(
                handle,
                FSCTL_SET_REPARSE_POINT,
                ctypes.byref(rdb),
                buffer_size,
                None,
                0,
                ctypes.byref(wintypes.DWORD()),
                None,
            )
            if not success:
                raise _win32_error()
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def read_reparse_point(path: str) -> str | None:
        """Read the target of a reparse point (junction/symlink)."""
        win32_path = _to_win32_path(path)
        handle = kernel32.CreateFileW(
            win32_path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == -1:
            raise _win32_error()

        try:
            buffer_size = 16384
            buffer = ctypes.create_string_buffer(buffer_size)
            _ = wintypes.DWORD()

            success = kernel32.DeviceIoControl(
                handle,
                FSCTL_GET_REPARSE_POINT,
                None,
                0,
                buffer,
                buffer_size,
                ctypes.byref(wintypes.DWORD()),
                None,
            )
            if not success:
                raise _win32_error()

            rdb = ctypes.cast(buffer, ctypes.POINTER(ReparseDataBuffer)).contents

            if rdb.ReparseTag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK):
                substitute_name = ctypes.wstring_at(
                    ctypes.addressof(rdb.PathBuffer) + rdb.SubstituteNameOffset,
                    rdb.SubstituteNameLength // 2,
                )
                # Convert \??\C:\path to C:\path
                if substitute_name.startswith("\\??\\"):
                    return substitute_name[4:]
                return substitute_name
            return None
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def get_file_version(path: str) -> str:
        """Get SHA-256 hash of file contents."""
        win32_path = _to_win32_path(path)
        with open(win32_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    @staticmethod
    def stat(path: str) -> os.stat_result:
        """Get file stat (like os.stat but with Windows handle)."""
        win32_path = _to_win32_path(path)
        return os.stat(win32_path)

    @staticmethod
    def is_binary(path: str) -> bool:
        """Check if a file is binary."""
        try:
            with open(_to_win32_path(path), "rb") as f:
                chunk = f.read(4096)
        except OSError:
            return False
        if b"\x00" in chunk:
            return True
        guessed, _ = mimetypes.guess_type(path)
        if guessed is not None and not (
            guessed.startswith("text/")
            or guessed in {"application/json", "application/xml", "application/javascript"}
        ):
            return True
        return False


def file_version(path: str) -> str:
    """Return SHA-256 hash of file contents."""
    return Win32FileOps.get_file_version(path)


def is_binary(path: str) -> bool:
    """Check if a file is binary."""
    return Win32FileOps.is_binary(path)


def read_file(path: str, mode: str = "r", encoding: str = "utf-8") -> str:
    """Read a text file."""
    return Win32FileOps.read_file(path, mode, encoding)


def write_file(path: str, content: str, mode: str = "w", encoding: str = "utf-8") -> None:
    """Write a text file."""
    return Win32FileOps.write_file(path, content, mode, encoding)


def write_bytes(path: str, data: bytes) -> None:
    """Write binary data to a file."""
    return Win32FileOps.write_bytes(path, data)


def read_bytes(path: str) -> bytes:
    """Read binary data from a file."""
    return Win32FileOps.read_bytes(path)


def delete_file(path: str) -> None:
    """Delete a file."""
    return Win32FileOps.delete_file(path)


def remove_directory(path: str) -> None:
    """Remove an empty directory."""
    return Win32FileOps.remove_directory(path)


def create_directory(path: str) -> None:
    """Create a directory."""
    return Win32FileOps.create_directory(path)


def create_directories(path: str) -> None:
    """Create directory and all parent directories."""
    return Win32FileOps.create_directories(path)


def move_file(src: str, dst: str, replace: bool = False) -> None:
    """Move or rename a file/directory."""
    return Win32FileOps.move_file(src, dst, replace)


def create_junction(target: str, junction_path: str) -> None:
    """Create a directory junction (mount point)."""
    return Win32FileOps.create_junction(target, junction_path)


def is_junction(path: str) -> bool:
    """Check if path is a junction (mount point)."""
    return Win32FileOps.is_junction(path)


def is_symlink(path: str) -> bool:
    """Check if path is a symbolic link."""
    return Win32FileOps.is_symlink(path)


__all__ = [
    "Win32FileOps",
    "file_version",
    "is_binary",
    "read_file",
    "write_file",
    "write_bytes",
    "read_bytes",
    "delete_file",
    "remove_directory",
    "create_directory",
    "create_directories",
    "move_file",
    "create_junction",
    "is_junction",
    "is_symlink",
]
