"""DeepSpace runtime manifest parity regression tests."""

from __future__ import annotations

import json
import os
import platform
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.deepspace.services.runtime_manifest import (
    RuntimeManifestError,
    copy_runtime_payload,
    load_runtime_manifest,
    validate_runtime_manifest,
    verify_runtime_payload,
    write_runtime_manifest,
)
from app.deepspace.services.spill_policy import (
    result_preview,
    retain_content_blocks,
    take_recovered_images,
)
from app.deepspace.services.spill_store import SpillStoreError
from app.deepspace.services.tools.dependencies import (
    _load_workspace_dependencies,
    workspace_dependencies,
)
from app.deepspace.services.tools.spill import _recover_spilled_images

pytestmark = pytest.mark.unit_no_db


def _settings(**values: Any) -> SimpleNamespace:
    defaults = {
        "deepspace_spill_backend": "local",
        "deepspace_spill_local_root": "",
        "deepspace_spill_retention_seconds": 90 * 86400,
        "deepspace_spill_max_chars": 2_000_000,
        "deepspace_spill_threshold_chars": 24_000,
        "minio_bucket": "unit-test",
        "minio_endpoint": "unused.invalid",
        "deepspace_skill_custom_roots": "",
        "deepspace_bundled_skills_enabled": False,
        "deepspace_skill_user_invocation_enabled": True,
        "deepspace_permission_mode": "workspace-write",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _runtime_fixture(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib" / "sample").mkdir(parents=True)
    python = root / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    package = root / "lib" / "sample" / "__init__.py"
    package.write_text("value = 1\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "platform": "linux" if sys.platform.startswith("linux") else sys.platform,
        "architecture": platform.machine(),
        "root": str(root),
        "python": {"version": platform.python_version(), "path": str(python)},
        "packages": {"sample": {"version": "1.0", "path": str(package.parent)}},
        "executables": {},
    }
    path = root / "runtime.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _allow_fake_runtime_interpreter_on_noexec_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat the test fixture's shell stub as executable on noexec tmpfs."""
    import app.deepspace.services.runtime_manifest as runtime_module

    real_access = runtime_module.os.access

    def access(path: str | os.PathLike[str], mode: int, **kwargs: Any) -> bool:
        if mode & os.X_OK and Path(path).name == "python":
            return True
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(runtime_module.os, "access", access)


def _runtime_fixture(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib" / "sample").mkdir(parents=True)
    python = root / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    package = root / "lib" / "sample" / "__init__.py"
    package.write_text("value = 1\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "platform": "linux" if sys.platform.startswith("linux") else sys.platform,
        "architecture": platform.machine(),
        "root": str(root),
        "python": {"version": platform.python_version(), "path": str(python)},
        "packages": {"sample": {"version": "1.0", "path": str(package.parent)}},
        "executables": {},
    }
    path = root / "runtime.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _allow_fake_runtime_interpreter_on_noexec_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat the test fixture's shell stub as executable on noexec tmpfs."""
    import app.deepspace.services.runtime_manifest as runtime_module

    real_access = runtime_module.os.access

    def access(path: str | os.PathLike[str], mode: int, **kwargs: Any) -> bool:
        if mode & os.X_OK and Path(path).name == "python":
            return True
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(runtime_module.os, "access", access)


def test_runtime_manifest_validates_platform_paths_and_copies_verified_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_fake_runtime_interpreter_on_noexec_mount(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    source_manifest = _runtime_fixture(source)
    loaded = load_runtime_manifest(source_manifest)
    assert loaded.packages["sample"]["version"] == "1.0"
    destination_manifest = copy_runtime_payload(source_manifest, tmp_path / "copy")
    copied = verify_runtime_payload(destination_manifest)
    assert Path(copied.python_path).is_file()
    assert (copied.root / "lib" / "sample" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "value = 1\n"
    with pytest.raises(RuntimeManifestError, match="already exists"):
        copy_runtime_payload(source_manifest, tmp_path / "copy")


def test_runtime_manifest_rejects_wrong_platform_and_outside_paths(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    manifest_path = _runtime_fixture(root)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    with pytest.raises(RuntimeManifestError, match="different operating system"):
        validate_runtime_manifest(data, root=root, expected_platform="not-this-platform")
    data["python"]["path"] = str(tmp_path / "outside-python")
    with pytest.raises(RuntimeManifestError, match="outside"):
        validate_runtime_manifest(data, root=root)


def test_runtime_payload_detects_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_fake_runtime_interpreter_on_noexec_mount(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    manifest = _runtime_fixture(source)
    copied = copy_runtime_payload(manifest, tmp_path / "copied")
    (copied.parent / "lib" / "sample" / "__init__.py").write_text("changed", encoding="utf-8")
    with pytest.raises(RuntimeManifestError, match="integrity"):
        verify_runtime_payload(copied)


def test_runtime_manifest_writer_requires_manifest_at_payload_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside_manifest = tmp_path / "outside" / "runtime.json"
    with pytest.raises(RuntimeManifestError, match="root of its payload"):
        write_runtime_manifest(outside_manifest, (), root=source)
    assert not outside_manifest.parent.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data, root: data.update(schema_version=2), "unsupported schema"),
        (lambda data, root: data.update(root=str(root.parent)), "does not match"),
        (lambda data, root: data.update(platform=""), "platform identity"),
        (lambda data, root: data.update(architecture=""), "platform identity"),
        (
            lambda data, root: data.update(architecture="different"),
            "different CPU architecture",
        ),
        (lambda data, root: data.update(python=[]), "no Python runtime"),
        (lambda data, root: data["python"].update(path="relative/python"), "outside"),
        (
            lambda data, root: data["python"].update(path=str(root / "missing")),
            "outside",
        ),
        (lambda data, root: data["python"].update(version=""), "Python version"),
        (lambda data, root: data.update(packages=[]), "package list"),
        (
            lambda data, root: data["packages"].update(sample={"version": ""}),
            "incomplete or outside",
        ),
        (
            lambda data, root: data["packages"].update(
                sample={"version": "1", "path": str(root.parent)}
            ),
            "incomplete or outside",
        ),
        (lambda data, root: data.update(executables=[]), "executable list"),
        (lambda data, root: data["executables"].update(node="relative/node"), "invalid executable"),
        (
            lambda data, root: data["executables"].update(node=str(root.parent / "outside")),
            "unavailable or outside",
        ),
    ],
)
def test_runtime_manifest_rejects_invalid_contract_fields(
    tmp_path: Path, mutation, message: str
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    manifest_path = _runtime_fixture(root)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(data, root)
    with pytest.raises(RuntimeManifestError, match=message):
        validate_runtime_manifest(
            data,
            root=root,
            expected_architecture=(
                platform.machine() if "different CPU architecture" in message else None
            ),
        )


def test_current_runtime_manifest_filters_unbundled_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    root = tmp_path / "runtime"
    root.mkdir()
    package = root / "lib" / "sample.py"
    package.parent.mkdir()
    package.write_text("value = 1", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("value = 2", encoding="utf-8")
    interpreter = root / "bin" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("python", encoding="utf-8")

    def find_spec(name: str) -> Any:
        if name == "missing":
            raise ModuleNotFoundError(name)
        return SimpleNamespace(
            origin=str(package if name == "inside" else outside),
            submodule_search_locations=None,
        )

    monkeypatch.setattr(runtime_module.sys, "prefix", str(root))
    monkeypatch.setattr(runtime_module.sys, "executable", str(interpreter))
    monkeypatch.setattr(runtime_module.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(runtime_module.importlib.metadata, "version", lambda name: "1.2")
    monkeypatch.setattr(
        runtime_module.shutil,
        "which",
        lambda name: str(root / "bin" / name) if name == "node" else str(outside),
    )
    (root / "bin" / "node").write_text("node", encoding="utf-8")

    data = runtime_module.current_runtime_manifest(
        (("inside-dist", "inside"), ("outside-dist", "outside"), ("missing-dist", "missing")),
        root=root,
    )

    assert data["packages"] == {"inside-dist": {"version": "1.2", "path": str(package)}}
    assert data["executables"] == {"node": str(root / "bin" / "node")}
    assert data["python"]["path"] == str(interpreter)


def test_runtime_manifest_writer_emits_atomic_valid_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    root = tmp_path / "payload"
    root.mkdir()
    source_manifest = _runtime_fixture(root)
    data = json.loads(source_manifest.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        runtime_module,
        "current_runtime_manifest",
        lambda distributions, *, root: data,
    )
    output = root / "written.json"

    write_runtime_manifest(output, (), root=root)

    assert load_runtime_manifest(output).root == root
    assert not list(root.glob(".written.json.*"))


def test_pyinstaller_manifest_lists_only_modules_included_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    monkeypatch.setattr(
        runtime_module.importlib.metadata,
        "version",
        lambda name: {"numpy": "2.0", "python-docx": "1.2"}[name],
    )
    manifest = runtime_module.pyinstaller_runtime_manifest(
        (("numpy", "numpy"), ("python-docx", "docx"), ("openpyxl", "openpyxl")),
        {"numpy", "numpy.linalg", "docx.api"},
        executable_name="averqel-backend",
    )

    assert manifest["root"] == "."
    assert manifest["python"]["path"] == "./averqel-backend"
    assert manifest["packages"] == {
        "numpy": {
            "version": "2.0",
            "path": "./averqel-backend",
            "module": "numpy",
            "storage": "pyinstaller-executable-archive",
        },
        "python-docx": {
            "version": "1.2",
            "path": "./averqel-backend",
            "module": "docx",
            "storage": "pyinstaller-executable-archive",
        },
    }


def test_frozen_runtime_manifest_resolves_paths_from_bundle_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    root = tmp_path / "package"
    internal = root / "_internal"
    internal.mkdir(parents=True)
    executable = root / "averqel-backend"
    executable.write_text("frozen application", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(runtime_module.importlib.metadata, "version", lambda _: "stdlib-test")
    manifest = runtime_module.pyinstaller_runtime_manifest(
        (("stdlib-test", "json"),), {"json"}, executable_name=executable.name
    )
    (internal / "runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(runtime_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_module.sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(runtime_module.sys, "executable", str(executable))
    monkeypatch.setattr(
        runtime_module.importlib.metadata,
        "version",
        lambda name: "stdlib-test" if name == "stdlib-test" else "missing",
    )

    loaded = load_runtime_manifest(internal / "runtime.json")

    assert loaded.root == root
    assert loaded.python_path == str(executable)
    assert loaded.packages["stdlib-test"]["storage"] == "pyinstaller-executable-archive"
    assert runtime_module.verify_frozen_runtime_manifest() == loaded


def test_frozen_runtime_verification_requires_a_packaged_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    monkeypatch.setattr(runtime_module.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeManifestError, match="requires a frozen application"):
        runtime_module.verify_frozen_runtime_manifest()


def test_frozen_dependency_discovery_fails_closed_without_its_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.tools.dependencies as dependencies

    monkeypatch.setattr(dependencies.sys, "frozen", True, raising=False)
    monkeypatch.setattr(dependencies.sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(RuntimeManifestError, match="manifest is missing"):
        workspace_dependencies(_settings())


def test_frozen_runtime_uses_host_office_binaries_only_without_custom_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module
    import app.deepspace.services.tools.dependencies as dependencies

    root = tmp_path / "bundle"
    internal = root / "_internal"
    internal.mkdir(parents=True)
    executable = root / "averqel-backend"
    executable.write_text("frozen app", encoding="utf-8")
    manifest = runtime_module.pyinstaller_runtime_manifest(
        (), set(), executable_name=executable.name
    )
    manifest_path = internal / "runtime.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(dependencies.sys, "frozen", True, raising=False)
    monkeypatch.setattr(dependencies.sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(dependencies.sys, "executable", str(executable))
    monkeypatch.setattr(dependencies, "_binary_path", lambda name: f"/host/bin/{name}")

    packaged_defaults = workspace_dependencies(_settings())
    explicit_manifest = workspace_dependencies(
        _settings(deepspace_runtime_manifest_path=str(manifest_path))
    )

    assert packaged_defaults["office_runtime"]["soffice"] == "/host/bin/soffice"
    assert packaged_defaults["office_runtime"]["pdftoppm"] == "/host/bin/pdftoppm"
    assert packaged_defaults["office_runtime_sources"]["soffice"] == "host-path"
    assert explicit_manifest["office_runtime"]["soffice"] is None
    assert explicit_manifest["office_runtime_sources"]["soffice"] == "unavailable"


@pytest.mark.asyncio
async def test_workspace_dependency_tool_uses_and_validates_runtime_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    manifest = _runtime_fixture(root)
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_data["packages"]["numpy"] = manifest_data["packages"].pop("sample")
    manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
    settings = _settings(deepspace_runtime_manifest_path=str(manifest))

    report = workspace_dependencies(settings)
    result = await _load_workspace_dependencies({}, SimpleNamespace(settings=settings))

    assert report["runtime"]["source"] == "manifest"
    assert report["python"] == str(root / "bin" / "python")
    assert report["python_packages"]["numpy"] == str(root / "lib" / "sample")
    assert report["python_package_versions"]["numpy"] == "1.0"
    assert report["python_package_storage"]["numpy"] == "filesystem"
    assert result["status"] == "ok"
    invalid = _settings(deepspace_runtime_manifest_path=str(tmp_path / "missing.json"))
    unavailable = await _load_workspace_dependencies({}, SimpleNamespace(settings=invalid))
    assert unavailable["status"] == "unavailable"
    assert unavailable["error_code"] == "RUNTIME_MANIFEST_INVALID"


def test_workspace_dependencies_reports_active_runtime_and_missing_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import dependencies

    lib = tmp_path / "numpy.py"
    lib.write_text("", encoding="utf-8")
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    monkeypatch.setattr(dependencies.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(
        dependencies,
        "_module_path",
        lambda name: str(lib) if name == "numpy" else None,
    )

    def missing_version(name: str) -> str:
        del name
        raise dependencies.importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(dependencies.importlib.metadata, "version", missing_version)
    monkeypatch.setattr(
        dependencies,
        "_binary_path",
        lambda name: str(node) if name == "node" else None,
    )

    report = dependencies.workspace_dependencies(SimpleNamespace())

    assert report["python_packages"] == {"numpy": str(lib)}
    assert report["python_package_versions"] == {"numpy": "unknown"}
    assert report["node"] == str(node)
    assert report["pnpm"] is None
    assert report["office_runtime"]["soffice"] is None
    assert report["runtime"]["source"] == "active-interpreter"
    assert report["runtime"]["platform"] == dependencies.sys.platform


def test_dependency_path_helpers_handle_missing_and_namespace_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import dependencies

    package_dir = tmp_path / "namespace"
    package_dir.mkdir()
    monkeypatch.setattr(
        dependencies.importlib.util,
        "find_spec",
        lambda name: (
            SimpleNamespace(origin=None, submodule_search_locations=[str(package_dir)])
            if name == "namespace"
            else None
        ),
    )
    assert dependencies._module_path("namespace") == str(package_dir)
    assert dependencies._module_path("missing") is None

    def broken_spec(name: str) -> Any:
        del name
        raise ValueError("partially initialized package")

    monkeypatch.setattr(dependencies.importlib.util, "find_spec", broken_spec)
    assert dependencies._module_path("broken") is None
    monkeypatch.setattr(dependencies.shutil, "which", lambda name: None)
    assert dependencies._binary_path("missing") is None
    monkeypatch.setattr(dependencies.shutil, "which", lambda name: str(tmp_path / "absent"))
    assert dependencies._binary_path("absent") is None


def test_runtime_payload_rejects_in_root_directory_symlink_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_fake_runtime_interpreter_on_noexec_mount(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    manifest = _runtime_fixture(source)
    sample = source / "lib" / "sample"
    (sample / "cycle").symlink_to(sample, target_is_directory=True)
    with pytest.raises(RuntimeManifestError, match="cycle"):
        copy_runtime_payload(manifest, tmp_path / "copy")


@pytest.mark.parametrize(
    ("system", "sys_platform", "expected"),
    [
        ("Linux", "linux", "linux"),
        ("Darwin", "darwin", "darwin"),
        ("Windows", "win32", "win32"),
        ("Plan9", "plan9", "plan9"),
    ],
)
def test_runtime_manifest_normalizes_platform_names(
    monkeypatch: pytest.MonkeyPatch, system: str, sys_platform: str, expected: str
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    monkeypatch.setattr(runtime_module.platform, "system", lambda: system)
    monkeypatch.setattr(runtime_module.sys, "platform", sys_platform)

    assert runtime_module._platform_name() == expected


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data, root: data.update(root="."), None),
        (lambda data, root: data["python"].update(path=str(root / "bin")), "not a file"),
        (lambda data, root: data.update(packages={"sample": []}), "invalid package entry"),
        (lambda data, root: data.update(executables={"tool": str(root / "lib")}), "unavailable"),
    ],
)
def test_runtime_manifest_checks_declared_file_kinds(
    tmp_path: Path, mutation: Any, message: str | None
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "lib").mkdir()
    manifest_path = _runtime_fixture(root)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(data, root)

    if message is None:
        assert validate_runtime_manifest(data, root=root).root == root.resolve()
    else:
        with pytest.raises(RuntimeManifestError, match=message):
            validate_runtime_manifest(data, root=root)


def test_runtime_manifest_validates_optional_metadata_and_unavailable_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    manifest_path = _runtime_fixture(root)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["packages"]["sample"].update(module="sample", storage="filesystem", path="./lib/sample")

    loaded = validate_runtime_manifest(data, root=root)

    assert loaded.packages["sample"]["path"] == str(root / "lib" / "sample")
    assert loaded.packages["sample"]["module"] == "sample"
    assert loaded.packages["sample"]["storage"] == "filesystem"
    with pytest.raises(RuntimeManifestError, match="root is unavailable"):
        validate_runtime_manifest(data, root=tmp_path / "missing-root")

    for optional_field, invalid_value in (("module", ""), ("storage", 7)):
        invalid = json.loads(json.dumps(data))
        invalid["packages"]["sample"][optional_field] = invalid_value
        with pytest.raises(RuntimeManifestError, match="invalid"):
            validate_runtime_manifest(invalid, root=root)


def test_current_runtime_manifest_handles_namespace_and_missing_package_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    root = tmp_path / "runtime"
    namespace = root / "lib" / "namespace"
    namespace.mkdir(parents=True)
    executable = root / "python"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("python", encoding="utf-8")
    missing_path = root / "missing"
    specs = {
        "namespace": SimpleNamespace(origin=None, submodule_search_locations=[str(namespace)]),
        "built-in": SimpleNamespace(origin="built-in", submodule_search_locations=None),
        "missing-path": SimpleNamespace(
            origin=None, submodule_search_locations=[str(missing_path)]
        ),
        "outside": SimpleNamespace(
            origin=str(tmp_path / "outside.py"), submodule_search_locations=None
        ),
        "absent": None,
    }
    monkeypatch.setattr(runtime_module.sys, "prefix", str(root))
    monkeypatch.setattr(runtime_module.sys, "executable", str(executable))
    monkeypatch.setattr(runtime_module.importlib.util, "find_spec", specs.__getitem__)

    def version(distribution: str) -> str:
        if distribution == "missing-version":
            raise runtime_module.importlib.metadata.PackageNotFoundError(distribution)
        return "1.0"

    monkeypatch.setattr(runtime_module.importlib.metadata, "version", version)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda _name: None)

    manifest = runtime_module.current_runtime_manifest(
        (
            ("namespace-dist", "namespace"),
            ("builtin-dist", "built-in"),
            ("missing-path-dist", "missing-path"),
            ("outside-dist", "outside"),
            ("missing-version", "namespace"),
            ("absent-dist", "absent"),
        ),
        root=root,
    )

    assert manifest["packages"] == {"namespace-dist": {"version": "1.0", "path": str(namespace)}}
    assert manifest["executables"] == {}


def test_runtime_manifest_writer_creates_default_payload_root(tmp_path: Path, monkeypatch) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    root = tmp_path / "created" / "runtime"
    root.mkdir(parents=True)
    interpreter = root / "python"
    interpreter.write_text("python", encoding="utf-8")
    monkeypatch.setattr(runtime_module.sys, "prefix", str(root))
    monkeypatch.setattr(runtime_module.sys, "executable", str(interpreter))
    output = root / "runtime.json"

    write_runtime_manifest(output, ())

    assert load_runtime_manifest(output).root == root.resolve()
    assert not list(root.glob(".runtime.json.*"))


def test_runtime_payload_rejects_external_symlinks_and_non_executable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    _allow_fake_runtime_interpreter_on_noexec_mount(monkeypatch)
    root = tmp_path / "source"
    root.mkdir()
    manifest = _runtime_fixture(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "lib" / "sample" / "outside").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeManifestError, match="leaves the source payload"):
        copy_runtime_payload(manifest, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()

    (root / "lib" / "sample" / "outside").unlink()
    real_access = runtime_module.os.access
    monkeypatch.setattr(
        runtime_module.os,
        "access",
        lambda path, mode, **kwargs: (
            False
            if mode & os.X_OK and Path(path).name == "python"
            else real_access(path, mode, **kwargs)
        ),
    )
    with pytest.raises(RuntimeManifestError, match="not executable"):
        verify_runtime_payload(manifest)


def test_frozen_runtime_verifier_rejects_missing_bundle_and_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.deepspace.services.runtime_manifest as runtime_module

    monkeypatch.setattr(runtime_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_module.sys, "_MEIPASS", None, raising=False)
    with pytest.raises(RuntimeManifestError, match="no runtime payload"):
        runtime_module.verify_frozen_runtime_manifest()

    root = tmp_path / "bundle"
    internal = root / "_internal"
    internal.mkdir(parents=True)
    executable = root / "averqel-backend"
    executable.write_text("frozen", encoding="utf-8")
    manifest = runtime_module.pyinstaller_runtime_manifest(
        (), set(), executable_name=executable.name
    )
    manifest["packages"] = {
        "missing": {
            "version": "1.0",
            "path": f"./{executable.name}",
            "module": "module_that_does_not_exist_for_deepspace_tests",
            "storage": "pyinstaller-executable-archive",
        }
    }
    (internal / "runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(runtime_module.sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(runtime_module.sys, "executable", str(executable))
    monkeypatch.setattr(runtime_module.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(RuntimeManifestError, match="dependency 'missing' is missing"):
        runtime_module.verify_frozen_runtime_manifest()


def test_content_preview_handles_unfittable_edges_and_mixed_payload_fallback() -> None:
    def counter(value: str) -> int:
        return 1 if "omitted" in value else max(1, len(value) * 100)

    preview = retain_content_blocks(
        [{"type": "text", "text": "head"}, {"type": "text", "text": "tail"}],
        2,
        token_counter=counter,
    )
    assert "omitted" in preview.preview
    assert preview.omitted_bytes == len(b"headtail")

    fallback = result_preview(
        {"content": [{"type": "text", "text": "structured"}, "not-a-block"]},
        "serialized fallback",
        100,
    )
    assert fallback.preview == "serialized fallback"

    malformed_images = {"_images_base64": "not-a-list"}
    assert take_recovered_images(malformed_images) == []
    mixed_images = {"_images_base64": ["YQ==", "", 3, None]}
    assert take_recovered_images(mixed_images) == ["YQ=="]


def test_image_recovery_supports_top_level_and_data_uri_payloads() -> None:
    payload = {
        "content": [
            {"type": "image", "data": "YQ=="},
            {"type": "image", "image_url": {"url": "data:image/jpeg;base64,Yg=="}},
        ]
    }

    display, images, omitted = _recover_spilled_images(payload)

    assert images == ["YQ==", "data:image/jpeg;base64,Yg=="]
    assert omitted == 0
    assert "image reattached" in json.dumps(display)


@pytest.mark.asyncio
async def test_spill_tools_convert_storage_errors_to_unavailable_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.deepspace.services.tools.spill as spill_tools

    class FailingStore:
        def __init__(self, settings: Any) -> None:
            del settings

        def get(self, *, tenant_id: uuid.UUID, sha256: str) -> str:
            del tenant_id, sha256
            raise SpillStoreError("storage offline")

        def put(self, *, tenant_id: uuid.UUID, payload: str) -> Any:
            del tenant_id, payload
            raise SpillStoreError("storage offline")

    class Result:
        def scalar_one_or_none(self) -> object:
            return object()

        def all(self) -> list[tuple[str, str]]:
            return [("user", "request")]

    class Database:
        def execute(self, statement: Any) -> Result:
            del statement
            return Result()

    monkeypatch.setattr(spill_tools, "SpillStore", FailingStore)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    context = SimpleNamespace(
        settings=_settings(),
        auth=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        db=Database(),
        conversation_id=uuid.uuid4(),
    )

    read_result = await spill_tools._spill_read(
        {"spill_ref": "A" * 64}, SimpleNamespace(settings=context.settings, auth=context.auth)
    )
    session_result = await spill_tools._spill_session({}, context)

    assert read_result == {
        "status": "unavailable",
        "message": "storage offline",
        "spill_ref": "a" * 64,
    }
    assert session_result == {"status": "unavailable", "message": "storage offline"}


def test_dependency_module_path_ignores_empty_namespace_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import dependencies

    absent = tmp_path / "absent"
    monkeypatch.setattr(
        dependencies.importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin=None, submodule_search_locations=[str(absent)]),
    )

    assert dependencies._module_path("empty_namespace") is None


@pytest.mark.asyncio
async def test_office_operation_rejects_oversized_input_and_existing_render_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import office

    docx = tmp_path / "source.docx"
    docx.write_bytes(b"large")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"pdf")
    render_dir = tmp_path / "source_pages"
    render_dir.mkdir()
    context = SimpleNamespace(
        workspace_root=tmp_path,
        settings=_settings(),
        sandbox_mode="workspace-write",
    )
    monkeypatch.setattr(office, "MAX_OFFICE_INPUT_BYTES", 1)
    monkeypatch.setattr(office, "_binary_paths", lambda _: ("soffice", "pdftoppm"))

    too_large = await office._office_process(
        {"operation": "export_pdf", "input_path": "source.docx"}, context
    )
    assert too_large["status"] == "refused"

    monkeypatch.setattr(office, "MAX_OFFICE_INPUT_BYTES", 100)
    existing_render = await office._office_process(
        {
            "operation": "render_pages",
            "input_path": "source.pdf",
            "output_path": "source_pages",
            "overwrite": True,
        },
        context,
    )
    outside_input = await office._office_process(
        {"operation": "export_pdf", "input_path": "../outside.docx"}, context
    )

    assert existing_render["status"] == "refused"
    assert "not replaced automatically" in existing_render["message"]
    assert outside_input["status"] == "error"
    assert outside_input["capability_failure"] is True
    assert outside_input["retryable"] is False


def test_office_atomic_copy_overwrites_only_when_requested(tmp_path: Path) -> None:
    from app.deepspace.services.tools.office import _atomic_copy

    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"new content")
    destination.write_bytes(b"old content")

    _atomic_copy(source, destination, overwrite=True)

    assert destination.read_bytes() == b"new content"
    assert not list(tmp_path.glob(".destination.bin.*"))


@pytest.mark.asyncio
async def test_office_render_cleans_staging_directory_after_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deepspace.services.tools import office

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    context = SimpleNamespace(
        workspace_root=tmp_path,
        settings=_settings(),
        sandbox_mode="workspace-write",
    )
    monkeypatch.setattr(office, "_binary_paths", lambda _: (None, "pdftoppm"))

    async def render(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str, str]:
        del cwd, timeout
        prefix = Path(argv[-1])
        prefix.with_name(prefix.name + "-1.png").write_bytes(b"page")
        return 0, "", ""

    monkeypatch.setattr(office, "_run_office", render)
    monkeypatch.setattr(
        office.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("publish failed"))
    )

    result = await office._office_process(
        {"operation": "render_pages", "input_path": "source.pdf"}, context
    )

    assert result["status"] == "error"
    assert "publish failed" in result["message"]
    assert not (tmp_path / "source_pages").exists()


@pytest.mark.parametrize("contents", ["{", "not-json"])
def test_runtime_manifest_loader_rejects_malformed_and_oversized_files(
    tmp_path: Path, contents: str
) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeManifestError, match="could not be read"):
        load_runtime_manifest(malformed)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 1_000_001)
    with pytest.raises(RuntimeManifestError, match="larger than one megabyte"):
        load_runtime_manifest(oversized)


def test_runtime_manifest_loader_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeManifestError, match="could not be read"):
        load_runtime_manifest(tmp_path / "missing.json")


def test_runtime_payload_verifier_rejects_invalid_hash_inventories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_fake_runtime_interpreter_on_noexec_mount(monkeypatch)
    root = tmp_path / "payload"
    root.mkdir()
    manifest_path = _runtime_fixture(root)
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    for inventory, message in (
        ([], "inventory is invalid"),
        ({"file": 1}, "inventory is invalid"),
        ({"missing": "a" * 64}, "missing or outside"),
        ({"bin/python": "0" * 64}, "failed integrity"),
    ):
        original["files_sha256"] = inventory
        manifest_path.write_text(json.dumps(original), encoding="utf-8")
        with pytest.raises(RuntimeManifestError, match=message):
            verify_runtime_payload(manifest_path)
