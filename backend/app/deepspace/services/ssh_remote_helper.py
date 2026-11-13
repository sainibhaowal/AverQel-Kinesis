"""Small stdlib-only filesystem helper embedded over an authenticated SSH channel."""

from __future__ import annotations

import base64
import binascii
import hashlib
import heapq
import hmac
import json
import os
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILE_BYTES = 1_000_000
MAX_RANGE_BYTES = 180_000
MAX_REQUEST_BYTES = MAX_FILE_BYTES * 2
MAX_DIRECTORY_ENTRIES = 10_000
MAX_DIRECTORY_PAGE_SIZE = 1_000
RPC_PROTOCOL_VERSION = 1


class RemoteFsError(ValueError):
    """A safe remote filesystem request could not be applied."""


def _safe_path(
    root: Path,
    raw: str,
    *,
    allow_missing_leaf: bool = False,
    allow_final_symlink: bool = False,
    create_parents: bool = False,
) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RemoteFsError("A relative remote path is required.")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"..", "."} for part in relative.parts):
        if str(relative) != ".":
            raise RemoteFsError("The remote path must stay inside the reviewed directory.")
    root_real = root.resolve(strict=True)
    if str(relative) == ".":
        if allow_missing_leaf and not allow_final_symlink:
            raise RemoteFsError("The reviewed directory itself cannot be created or replaced.")
        return root_real
    candidate = root_real.joinpath(*relative.parts)
    if allow_missing_leaf:
        if create_parents:
            parent = root_real
            for part in candidate.parent.relative_to(root_real).parts:
                parent = parent / part
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    try:
                        parent.mkdir(mode=0o700)
                    except FileExistsError:
                        pass
                    info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RemoteFsError("A remote parent directory is not a real directory.")
            parent = parent.resolve(strict=True)
        else:
            parent = candidate.parent.resolve(strict=True)
        if not parent.is_relative_to(root_real):
            raise RemoteFsError("The remote path leaves the reviewed directory.")
        candidate = parent / candidate.name
        if candidate.is_symlink() and not allow_final_symlink:
            raise RemoteFsError("The destination may not be a symbolic link.")
    else:
        candidate = candidate.resolve(strict=True)
        if not candidate.is_relative_to(root_real):
            raise RemoteFsError("The remote path leaves the reviewed directory.")
    return candidate


def _atomic_write(path: Path, payload: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _apply_line_patch(content: str, changes: Any) -> str:
    if not isinstance(changes, list) or not changes:
        raise RemoteFsError("A patch requires at least one line change.")
    lines = content.splitlines(keepends=True)
    normalized: list[tuple[int, int, list[str]]] = []
    for change in changes:
        if not isinstance(change, dict):
            raise RemoteFsError("Each patch change must be an object.")
        start = change.get("start_line")
        delete = change.get("delete_lines", 0)
        insert = change.get("insert_lines", [])
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or start < 1
            or not isinstance(delete, int)
            or isinstance(delete, bool)
            or delete < 0
            or not isinstance(insert, list)
            or any(not isinstance(line, str) for line in insert)
        ):
            raise RemoteFsError("A patch contains invalid line coordinates or content.")
        index = start - 1
        if index > len(lines) or index + delete > len(lines):
            raise RemoteFsError("A patch line range is outside the file.")
        normalized.append((index, delete, insert))
    starts = [index for index, _, _ in normalized]
    if len(starts) != len(set(starts)):
        raise RemoteFsError("A patch cannot contain multiple changes at the same line.")
    ordered = sorted(normalized, key=lambda item: item[0], reverse=True)
    previous_start = len(lines) + 1
    for index, delete, insert in ordered:
        if index + delete > previous_start:
            raise RemoteFsError("Patch line ranges overlap.")
        previous_start = index
        replacement = [line if line.endswith(("\n", "\r")) else line + "\n" for line in insert]
        lines[index : index + delete] = replacement
    return "".join(lines)


def dispatch_fs_request(root: Path, request: Any) -> dict[str, Any]:
    """Apply one validated file operation below the reviewed root."""
    if not isinstance(request, dict):
        raise RemoteFsError("The remote filesystem request is invalid.")
    operation = request.get("operation")
    raw_path = request.get("path")
    if not isinstance(raw_path, str):
        raise RemoteFsError("A relative remote path is required.")
    if operation == "lstat":
        path = _safe_path(root, raw_path, allow_missing_leaf=True, allow_final_symlink=True)
        lstat_result = path.lstat()
        kind = (
            "symlink"
            if stat.S_ISLNK(lstat_result.st_mode)
            else (
                "directory"
                if stat.S_ISDIR(lstat_result.st_mode)
                else "file" if stat.S_ISREG(lstat_result.st_mode) else "other"
            )
        )
        return {
            "path": str(path.relative_to(root.resolve())),
            "kind": kind,
            "size_bytes": lstat_result.st_size,
        }
    if operation == "stat":
        path = _safe_path(root, raw_path)
        stat_result = path.stat()
        kind = (
            "directory"
            if stat.S_ISDIR(stat_result.st_mode)
            else "file" if stat.S_ISREG(stat_result.st_mode) else "other"
        )
        return {
            "path": str(path.relative_to(root.resolve())),
            "kind": kind,
            "size_bytes": stat_result.st_size,
        }
    if operation == "read_range":
        path = _safe_path(root, raw_path)
        if not path.is_file():
            raise RemoteFsError("The remote path is not a file.")
        offset = request.get("offset", 0)
        length = request.get("max_bytes", MAX_RANGE_BYTES)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise RemoteFsError("The byte offset must be a non-negative integer.")
        if (
            not isinstance(length, int)
            or isinstance(length, bool)
            or not 1 <= length <= MAX_RANGE_BYTES
        ):
            raise RemoteFsError(f"A byte range must contain between 1 and {MAX_RANGE_BYTES} bytes.")
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(length)
        digest = None
        if size <= MAX_FILE_BYTES:
            hasher = hashlib.sha256()
            with path.open("rb") as source_stream:
                for block in iter(lambda: source_stream.read(128 * 1024), b""):
                    hasher.update(block)
            digest = hasher.hexdigest()
        return {
            "path": str(path.relative_to(root.resolve())),
            "offset": offset,
            "total_bytes": size,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
            "sha256": digest,
            "next_offset": offset + len(chunk) if offset + len(chunk) < size else None,
        }
    if operation == "list":
        path = _safe_path(root, raw_path)
        if not path.is_dir():
            raise RemoteFsError("The remote path is not a directory.")
        offset = request.get("offset", 0)
        limit = request.get("limit", 200)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise RemoteFsError("The directory offset must be a non-negative integer.")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_DIRECTORY_PAGE_SIZE
        ):
            raise RemoteFsError(
                f"A directory page must contain between 1 and {MAX_DIRECTORY_PAGE_SIZE} entries."
            )
        with os.scandir(path) as scan:
            entries = heapq.nsmallest(
                MAX_DIRECTORY_ENTRIES + 1,
                scan,
                key=lambda item: (item.name.casefold(), item.name),
            )
        truncated = len(entries) > MAX_DIRECTORY_ENTRIES
        entries = entries[:MAX_DIRECTORY_ENTRIES]
        page = entries[offset : offset + limit]
        results: list[dict[str, Any]] = []
        for directory_entry in page:
            info = directory_entry.stat(follow_symlinks=False)
            kind = (
                "symlink"
                if stat.S_ISLNK(info.st_mode)
                else (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file" if stat.S_ISREG(info.st_mode) else "other"
                )
            )
            results.append({"name": directory_entry.name, "kind": kind, "size_bytes": info.st_size})
        next_offset = offset + len(page) if offset + len(page) < len(entries) else None
        return {
            "path": str(path.relative_to(root.resolve())),
            "entries": results,
            "offset": offset,
            "next_offset": next_offset,
            "truncated": truncated,
            "total_entries": len(entries),
        }
    if operation == "write_bytes":
        path = _safe_path(root, raw_path, allow_missing_leaf=True, create_parents=True)
        encoded = request.get("content_base64")
        if not isinstance(encoded, str):
            raise RemoteFsError("The file content must be base64 text.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RemoteFsError("The file content is not valid base64.") from exc
        if len(content) > MAX_FILE_BYTES:
            raise RemoteFsError(f"A remote file may be at most {MAX_FILE_BYTES} bytes.")
        _atomic_write(path, content)
        return {"path": str(path.relative_to(root.resolve())), "size_bytes": len(content)}
    if operation == "move":
        source_path = _safe_path(root, raw_path, allow_missing_leaf=True)
        if not source_path.exists():
            raise FileNotFoundError("The remote source path does not exist.")
        destination = _safe_path(
            root, str(request.get("destination") or ""), allow_missing_leaf=True
        )
        if source_path == root.resolve() or destination == root.resolve():
            raise RemoteFsError("The reviewed directory itself cannot be moved.")
        if source_path.is_dir() and destination.is_relative_to(source_path):
            raise RemoteFsError("A directory cannot be moved into itself.")
        if destination.exists():
            raise RemoteFsError("The move destination already exists.")
        os.replace(source_path, destination)
        return {
            "path": str(source_path.relative_to(root.resolve())),
            "destination": str(destination.relative_to(root.resolve())),
        }
    if operation == "delete":
        path = _safe_path(root, raw_path, allow_missing_leaf=True, allow_final_symlink=True)
        entry = path.lstat()
        if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
            raise RemoteFsError("Only files and symbolic links can be deleted.")
        relative = str(path.relative_to(root.resolve()))
        path.unlink()
        return {"path": relative, "deleted": True}
    if operation in {"replace_text", "patch_lines"}:
        path = _safe_path(root, raw_path)
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise RemoteFsError("The remote file is unavailable or exceeds the edit limit.")
        raw = path.read_bytes()
        expected = request.get("expected_sha256")
        actual = hashlib.sha256(raw).hexdigest()
        if not isinstance(expected, str) or not hmac.compare_digest(expected, actual):
            raise RemoteFsError(
                "The remote file changed since it was read; reload it before editing."
            )
        try:
            decoded_content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RemoteFsError("Text editing only supports UTF-8 files.") from exc
        if operation == "replace_text":
            old = request.get("old_text")
            new = request.get("new_text")
            count = request.get("expected_occurrences", 1)
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise RemoteFsError(
                    "The replacement requires nonempty old text and string new text."
                )
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 1
                or decoded_content.count(old) != count
            ):
                raise RemoteFsError("The old text did not occur the expected number of times.")
            updated = decoded_content.replace(old, new)
        else:
            updated = _apply_line_patch(decoded_content, request.get("changes"))
        encoded = updated.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise RemoteFsError(f"An edited remote file may be at most {MAX_FILE_BYTES} bytes.")
        _atomic_write(path, encoded)
        return {
            "path": str(path.relative_to(root.resolve())),
            "size_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    raise RemoteFsError("The remote filesystem operation is not supported.")


def _rpc_response(root: Path, request: Any, helper_sha256: str) -> dict[str, Any]:
    """Validate and dispatch one JSON-RPC request frame."""
    if not isinstance(request, dict) or not isinstance(request.get("id"), str):
        raise RemoteFsError("The RPC request needs a string id.")
    if request.get("protocol_version") != RPC_PROTOCOL_VERSION:
        raise RemoteFsError("The RPC protocol version is unsupported.")
    method = request.get("method")
    params = request.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RemoteFsError("RPC parameters must be an object.")
    if method == "initialize":
        if params.get("protocol_version") != RPC_PROTOCOL_VERSION:
            raise RemoteFsError("The requested RPC protocol version is unsupported.")
        if params.get("helper_sha256") != helper_sha256:
            raise RemoteFsError("The helper bootstrap hash did not match.")
        result: dict[str, Any] = {
            "protocol_version": RPC_PROTOCOL_VERSION,
            "helper_sha256": helper_sha256,
            "capabilities": [
                "fs.stat",
                "fs.lstat",
                "fs.list",
                "fs.read_range",
                "fs.write_bytes",
                "fs.delete",
                "fs.move",
                "fs.replace_text",
                "fs.patch_lines",
            ],
        }
    elif method == "ping":
        result = {"pong": True, "protocol_version": RPC_PROTOCOL_VERSION}
    elif method == "fs":
        try:
            result = {"status": "ok", **dispatch_fs_request(root, params.get("request"))}
        except FileNotFoundError as exc:
            result = {"status": "error", "error_code": "SSH_NOT_FOUND", "message": str(exc)}
        except PermissionError:
            result = {
                "status": "error",
                "error_code": "SSH_PERMISSION_DENIED",
                "message": "The remote host denied access.",
            }
        except RemoteFsError as exc:
            result = {"status": "error", "error_code": "SSH_REMOTE_FS_ERROR", "message": str(exc)}
        except (TypeError, KeyError, IndexError):
            result = {
                "status": "error",
                "error_code": "SSH_REMOTE_FS_ERROR",
                "message": "The remote filesystem request is malformed.",
            }
    elif method == "shutdown":
        result = {"shutdown": True}
    else:
        raise RemoteFsError("The RPC method is not supported.")
    return {"id": request["id"], "result": result}


def serve_rpc(root: Path, helper_sha256: str) -> int:
    """Serve bounded newline-delimited JSON requests over SSH stdio."""
    while True:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not raw:
            return 0
        request_id: str | None = None
        try:
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise RemoteFsError("The RPC frame exceeds the request size limit.")
            request = json.loads(raw)
            if isinstance(request, dict) and isinstance(request.get("id"), str):
                request_id = request["id"]
            response = _rpc_response(root, request, helper_sha256)
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
            if request.get("method") == "shutdown":
                return 0
        except (OSError, RemoteFsError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            response = {
                "id": request_id or "",
                "error": {"code": "SSH_REMOTE_FS_ERROR", "message": str(exc)[:2_000]},
            }
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def main() -> int:
    """Serve RPC frames or process one legacy JSON request."""
    try:
        if len(sys.argv) >= 4 and sys.argv[1] == "--rpc":
            return serve_rpc(Path(sys.argv[2]).resolve(strict=True), str(sys.argv[3]))
        root = Path(sys.argv[1]).resolve(strict=True)
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise RemoteFsError("The remote filesystem request exceeds its size limit.")
        request = json.loads(raw)
        response = {"status": "ok", **dispatch_fs_request(root, request)}
    except FileNotFoundError as exc:
        response = {"status": "error", "error_code": "SSH_NOT_FOUND", "message": str(exc)}
    except PermissionError as exc:
        response = {"status": "error", "error_code": "SSH_PERMISSION_DENIED", "message": str(exc)}
    except (OSError, RemoteFsError, json.JSONDecodeError, IndexError) as exc:
        response = {"status": "error", "error_code": "SSH_REMOTE_FS_ERROR", "message": str(exc)}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_RANGE_BYTES",
    "MAX_REQUEST_BYTES",
    "RPC_PROTOCOL_VERSION",
    "RemoteFsError",
    "dispatch_fs_request",
    "main",
    "serve_rpc",
]
