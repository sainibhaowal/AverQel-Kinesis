"""Validated description of the Python runtime shipped with DeepSpace."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RuntimeManifestError(ValueError):
    """A runtime manifest is malformed, incompatible, or points outside its root."""


RUNTIME_PACKAGE_DISTRIBUTIONS: tuple[tuple[str, str], ...] = (
    ("numpy", "numpy"),
    ("pandas", "pandas"),
    ("python-docx", "docx"),
    ("python-pptx", "pptx"),
    ("openpyxl", "openpyxl"),
    ("Pillow", "PIL"),
    ("lxml", "lxml"),
    ("XlsxWriter", "xlsxwriter"),
)


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Validated platform and dependency locations for one Python runtime."""

    platform: str
    architecture: str
    python_version: str
    python_path: str
    packages: dict[str, dict[str, str]]
    executables: dict[str, str]
    root: Path


def _platform_name() -> str:
    system = platform.system().casefold()
    if system == "linux":
        return "linux"
    if system == "darwin":
        return "darwin"
    if system == "windows":
        return "win32"
    return sys.platform


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _manifest_path(value: Any, root: Path) -> Path:
    """Resolve one absolute or bundle-relative path from a manifest."""
    raw = str(value or "").strip()
    if not raw:
        return Path()
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def validate_runtime_manifest(
    value: Any,
    *,
    root: Path,
    expected_platform: str | None = None,
    expected_architecture: str | None = None,
) -> RuntimeManifest:
    """Validate manifest fields and ensure every declared path stays in its root."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeManifestError("The runtime manifest has an unsupported schema.")
    manifest_root = _manifest_path(value.get("root") or root, root)
    if not manifest_root.is_absolute():
        manifest_root = root / manifest_root
    try:
        manifest_root = manifest_root.resolve(strict=True)
        expected_root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeManifestError("The runtime manifest root is unavailable.") from exc
    if manifest_root != expected_root:
        raise RuntimeManifestError(
            "The runtime manifest root does not match its payload directory."
        )
    platform_name = str(value.get("platform") or "")
    architecture = str(value.get("architecture") or "")
    if not platform_name or not architecture:
        raise RuntimeManifestError("The runtime manifest is missing its platform identity.")
    if expected_platform and platform_name != expected_platform:
        raise RuntimeManifestError("The runtime manifest targets a different operating system.")
    if expected_architecture and architecture.casefold() != expected_architecture.casefold():
        raise RuntimeManifestError("The runtime manifest targets a different CPU architecture.")
    python_value = value.get("python")
    if not isinstance(python_value, dict):
        raise RuntimeManifestError("The runtime manifest has no Python runtime entry.")
    python_path = _manifest_path(python_value.get("path"), manifest_root)
    if not python_value.get("path") or not _inside(python_path, manifest_root):
        raise RuntimeManifestError("The Python executable is missing or outside the runtime root.")
    if not python_path.resolve(strict=True).is_file():
        raise RuntimeManifestError("The Python executable is not a file.")
    python_version = str(python_value.get("version") or "")
    if not python_version:
        raise RuntimeManifestError("The runtime manifest is missing the Python version.")
    raw_packages = value.get("packages", {})
    if not isinstance(raw_packages, dict):
        raise RuntimeManifestError("The runtime manifest package list is invalid.")
    packages: dict[str, dict[str, str]] = {}
    for name, raw in raw_packages.items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            raise RuntimeManifestError("The runtime manifest contains an invalid package entry.")
        version = str(raw.get("version") or "")
        package_path = _manifest_path(raw.get("path"), manifest_root)
        if not version or not raw.get("path") or not _inside(package_path, manifest_root):
            raise RuntimeManifestError(
                f"The runtime package {name!r} is incomplete or outside its root."
            )
        entry = {"version": version, "path": str(package_path.resolve(strict=True))}
        for optional_field in ("module", "storage"):
            optional_value = raw.get(optional_field)
            if optional_value is not None:
                if not isinstance(optional_value, str) or not optional_value:
                    raise RuntimeManifestError(
                        f"The runtime package {name!r} has invalid {optional_field} metadata."
                    )
                entry[optional_field] = optional_value
        packages[name] = entry
    raw_executables = value.get("executables", {})
    if not isinstance(raw_executables, dict):
        raise RuntimeManifestError("The runtime manifest executable list is invalid.")
    executables: dict[str, str] = {}
    for name, raw_path in raw_executables.items():
        executable = Path(str(raw_path or "")).expanduser()
        if not isinstance(name, str) or not name or not executable.is_absolute():
            raise RuntimeManifestError("The runtime manifest contains an invalid executable entry.")
        if not _inside(executable, manifest_root) or not executable.resolve(strict=True).is_file():
            raise RuntimeManifestError(
                f"The executable {name!r} is unavailable or outside its root."
            )
        executables[name] = str(executable.resolve(strict=True))
    return RuntimeManifest(
        platform=platform_name,
        architecture=architecture,
        python_version=python_version,
        python_path=str(python_path.resolve(strict=True)),
        packages=packages,
        executables=executables,
        root=manifest_root,
    )


def current_runtime_manifest(
    package_distributions: tuple[tuple[str, str], ...], *, root: Path | None = None
) -> dict[str, Any]:
    """Describe importable packages and available tools in the running runtime."""
    runtime_root = Path(root or sys.prefix).resolve()
    packages: dict[str, dict[str, str]] = {}
    for distribution, module_name in package_distributions:
        try:
            spec = importlib.util.find_spec(module_name)
            version = importlib.metadata.version(distribution)
        except (
            ImportError,
            ModuleNotFoundError,
            ValueError,
            importlib.metadata.PackageNotFoundError,
        ):
            continue
        if spec is None:
            continue
        origin = spec.origin
        search_paths = tuple(spec.submodule_search_locations or ())
        candidate = Path(origin) if origin and origin not in {"built-in", "frozen"} else None
        if candidate is None or not candidate.exists():
            candidate = next((Path(item) for item in search_paths if Path(item).exists()), None)
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(runtime_root):
            continue
        packages[distribution] = {"version": version, "path": str(resolved)}
    executables: dict[str, str] = {}
    for name in ("node", "pnpm", "soffice", "libreoffice", "pdftoppm"):
        path = shutil.which(name)
        if path:
            resolved = Path(path).resolve()
            if resolved.is_relative_to(runtime_root):
                executables[name] = str(resolved)
    return {
        "schema_version": 1,
        "platform": _platform_name(),
        "architecture": platform.machine(),
        "root": str(runtime_root),
        "python": {
            "version": platform.python_version(),
            "path": str(Path(sys.executable).absolute()),
        },
        "packages": packages,
        "executables": executables,
    }


def load_runtime_manifest(path: Path) -> RuntimeManifest:
    """Load and validate a configured runtime manifest from disk."""
    try:
        if path.stat().st_size > 1_000_000:
            raise RuntimeManifestError("The runtime manifest is larger than one megabyte.")
        value = json.loads(path.read_text(encoding="utf-8"))
    except RuntimeManifestError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeManifestError("The configured runtime manifest could not be read.") from exc
    root = path.parent
    bundle_directory = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_directory:
        try:
            if path.parent.resolve() == Path(bundle_directory).resolve():
                root = Path(sys.executable).resolve().parent
        except (OSError, RuntimeError):
            pass
    return validate_runtime_manifest(
        value,
        root=root,
        expected_platform=_platform_name(),
        expected_architecture=platform.machine(),
    )


def pyinstaller_runtime_manifest(
    package_distributions: tuple[tuple[str, str], ...],
    included_modules: set[str] | frozenset[str],
    *,
    executable_name: str,
) -> dict[str, Any]:
    """Describe dependencies embedded in a PyInstaller onedir executable.

    PyInstaller stores pure Python modules in its executable archive instead of
    individual filesystem directories. Package paths therefore point to the
    executable and identify that storage explicitly; packages not present in
    the analysis graph are omitted.
    """
    modules = set(included_modules)
    packages: dict[str, dict[str, str]] = {}
    for distribution, module_name in package_distributions:
        if not any(
            candidate == module_name or candidate.startswith(f"{module_name}.")
            for candidate in modules
        ):
            continue
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
        packages[distribution] = {
            "version": version,
            "path": f"./{executable_name}",
            "module": module_name,
            "storage": "pyinstaller-executable-archive",
        }
    return {
        "schema_version": 1,
        "platform": _platform_name(),
        "architecture": platform.machine(),
        "root": ".",
        "python": {
            "version": platform.python_version(),
            "path": f"./{executable_name}",
        },
        "packages": packages,
        "executables": {},
    }


def verify_frozen_runtime_manifest() -> RuntimeManifest:
    """Validate the manifest and embedded module inventory of a frozen app."""
    if not getattr(sys, "frozen", False):
        raise RuntimeManifestError("Packaged runtime verification requires a frozen application.")
    bundle_directory = getattr(sys, "_MEIPASS", None)
    if not bundle_directory:
        raise RuntimeManifestError("The frozen application has no runtime payload directory.")
    manifest = load_runtime_manifest(Path(bundle_directory) / "runtime.json")
    executable = Path(sys.executable).resolve()
    if Path(manifest.python_path) != executable:
        raise RuntimeManifestError(
            "The runtime manifest does not identify this application binary."
        )
    for name, package in manifest.packages.items():
        module_name = package.get("module")
        if module_name:
            try:
                spec = importlib.util.find_spec(module_name)
            except (ImportError, ModuleNotFoundError, ValueError) as exc:
                raise RuntimeManifestError(
                    f"The bundled dependency {name!r} cannot be resolved."
                ) from exc
            if spec is None:
                raise RuntimeManifestError(f"The bundled dependency {name!r} is missing.")
    return manifest


def write_runtime_manifest(
    path: Path,
    package_distributions: tuple[tuple[str, str], ...],
    *,
    root: Path | None = None,
) -> None:
    """Write a manifest for a self-contained Python runtime payload."""
    path = path.expanduser().absolute()
    if root is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        runtime_root = path.parent.resolve(strict=True)
    else:
        runtime_root = Path(root).expanduser().resolve(strict=True)
        if path.parent.resolve() != runtime_root:
            raise RuntimeManifestError(
                "The runtime manifest must be stored at the root of its payload."
            )
    data = current_runtime_manifest(package_distributions, root=runtime_root)
    validate_runtime_manifest(data, root=runtime_root)
    payload = json.dumps(data, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_tree_checked(
    source: Path,
    target: Path,
    source_root: Path,
    *,
    ancestors: frozenset[Path] = frozenset(),
) -> None:
    """Copy a runtime subtree while dereferencing only in-root symlinks."""
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeManifestError(
            "A runtime dependency is missing or has a symlink cycle."
        ) from exc
    if not resolved.is_relative_to(source_root):
        raise RuntimeManifestError("A runtime dependency symlink leaves the source payload.")
    if resolved.is_dir():
        if resolved in ancestors:
            raise RuntimeManifestError("A runtime dependency contains a directory symlink cycle.")
        descendants = ancestors | {resolved}
        target.mkdir(parents=True, exist_ok=True)
        for child in sorted(resolved.iterdir(), key=lambda item: item.name):
            _copy_tree_checked(child, target / child.name, source_root, ancestors=descendants)
        try:
            shutil.copystat(resolved, target, follow_symlinks=True)
        except OSError:
            pass
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved, target)


def verify_runtime_payload(manifest_path: Path) -> RuntimeManifest:
    """Validate a packaged runtime and its optional file-hash inventory."""
    manifest = load_runtime_manifest(manifest_path)
    if not Path(manifest.python_path).is_file() or not os.access(manifest.python_path, os.X_OK):
        raise RuntimeManifestError("The packaged Python interpreter is not executable.")
    for name, package in manifest.packages.items():
        if not Path(package["path"]).exists():
            raise RuntimeManifestError(f"The packaged dependency {name!r} is missing.")
    for name, raw_path in manifest.executables.items():
        if not os.access(raw_path, os.X_OK):
            raise RuntimeManifestError(f"The packaged executable {name!r} is not executable.")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeManifestError(
            "The runtime manifest could not be re-read for verification."
        ) from exc
    hashes = raw.get("files_sha256", {})
    if not isinstance(hashes, dict):
        raise RuntimeManifestError("The runtime file hash inventory is invalid.")
    for relative, expected in hashes.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise RuntimeManifestError("The runtime file hash inventory is invalid.")
        try:
            candidate = (manifest.root / relative).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeManifestError(
                "A hashed runtime file is missing or outside its root."
            ) from exc
        if not candidate.is_relative_to(manifest.root) or not candidate.is_file():
            raise RuntimeManifestError("A hashed runtime file is missing or outside its root.")
        if _sha256_file(candidate) != expected:
            raise RuntimeManifestError(
                f"The packaged runtime file {relative!r} failed integrity validation."
            )
    return manifest


def copy_runtime_payload(source_manifest: Path, destination: Path) -> Path:
    """Copy declared runtime packages and binaries into a new verified payload.

    Only paths named by the manifest are copied. Existing destinations are
    never overwritten. Symlinks are dereferenced only when their final target
    remains inside the source runtime, then every copied regular file is
    recorded in the destination manifest's SHA-256 inventory.
    """
    source = verify_runtime_payload(source_manifest)
    source_root = source.root.resolve(strict=True)
    destination = destination.expanduser().absolute()
    if destination.exists():
        raise RuntimeManifestError("The runtime payload destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        declared: set[Path] = {Path(source.python_path)}
        declared.update(Path(item["path"]) for item in source.packages.values())
        declared.update(Path(item) for item in source.executables.values())
        copied_roots: list[Path] = []
        for original in sorted(declared, key=lambda item: (len(item.parts), str(item))):
            original = original.absolute()
            try:
                relative = original.relative_to(source_root)
            except ValueError as exc:
                raise RuntimeManifestError(
                    "A runtime dependency is outside its source payload."
                ) from exc
            if any(
                original == existing or existing in original.parents for existing in copied_roots
            ):
                continue
            _copy_tree_checked(original, staging / relative, source_root)
            copied_roots.append(original)

        hashes: dict[str, str] = {}
        for copied in sorted(staging.rglob("*")):
            if copied.is_file() and not copied.is_symlink():
                hashes[copied.relative_to(staging).as_posix()] = _sha256_file(copied)

        def path_in_payload(value: str) -> str:
            original = Path(value).absolute()
            try:
                relative = original.relative_to(source_root)
            except ValueError as exc:
                raise RuntimeManifestError(
                    "A runtime path cannot be mapped into the copied payload."
                ) from exc
            return str(destination / relative)

        payload = {
            "schema_version": 1,
            "platform": source.platform,
            "architecture": source.architecture,
            "root": str(destination),
            "python": {
                "version": source.python_version,
                "path": path_in_payload(source.python_path),
            },
            "packages": {
                name: {"version": item["version"], "path": path_in_payload(item["path"])}
                for name, item in source.packages.items()
            },
            "executables": {
                name: path_in_payload(path) for name, path in source.executables.items()
            },
            "files_sha256": hashes,
        }
        manifest_name = source_manifest.name
        (staging / manifest_name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, destination)
        copied_manifest = destination / manifest_name
        verify_runtime_payload(copied_manifest)
        return copied_manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise


__all__ = [
    "RuntimeManifest",
    "RuntimeManifestError",
    "copy_runtime_payload",
    "current_runtime_manifest",
    "load_runtime_manifest",
    "validate_runtime_manifest",
    "verify_runtime_payload",
    "write_runtime_manifest",
]
