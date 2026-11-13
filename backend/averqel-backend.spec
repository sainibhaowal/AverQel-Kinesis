# PyInstaller build spec for the complete local FastAPI runtime.
# This must be evaluated on the target OS and CPU architecture.
import atexit
import json
import os
from importlib.util import find_spec
from pathlib import Path
import sys
import tempfile

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

backend_dir = Path(SPECPATH).resolve()
sys.path.insert(0, str(backend_dir))

# FastAPI routes and provider implementations are partly imported by string, so
# include all app modules. MCP catalog seeding is also loaded dynamically.
hiddenimports = collect_submodules("app", on_error="warn")
hiddenimports += ["app.main", "scripts.seed_mcp_catalog"]
datas = collect_data_files("app")
# The SSH client embeds this standalone stdlib helper on the remote host.
# PyInstaller archives Python modules as bytecode, so keep the source as data
# and resolve it from _MEIPASS at runtime.
datas += [(str(backend_dir / "app/deepspace/services/ssh_remote_helper.py"), "app/deepspace/services")]
binaries = []

# These packages load native extensions, tokenizer/model metadata, or plugin
# modules dynamically. collect_all preserves those pieces in the frozen bundle.
runtime_packages = (
    "torch",
    "sentence_transformers",
    "transformers",
    "huggingface_hub",
    "tokenizers",
    "fakeredis",
    "onnxruntime",
    "mcp",
    "playwright",
    # Celery imports fixups, task classes, and transports by dotted name.
    "celery",
    "composio",
    "googleapiclient",
    "duckdb",
    "polars",
    "pypdfium2",
    "sqlite_vec",
)

for package in runtime_packages:
    if find_spec(package) is None:
        continue
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# OCR is an optional platform capability. The main application works without
# PaddleOCR; include it on platforms where its wheel is available.
for package in ("paddle", "paddleocr"):
    if find_spec(package) is not None:
        package_datas, package_binaries, package_hiddenimports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hiddenimports

a = Analysis(
    [str(backend_dir / "run_backend.py")],
    pathex=[str(backend_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests", "test"],
    noarchive=False,
    optimize=1,
)

# Ship a platform-specific manifest alongside the frozen payload. At runtime,
# relative paths resolve to the bundle root, and verification checks that every
# listed dependency exists in the PyInstaller module graph.
from app.deepspace.services.runtime_manifest import (
    RUNTIME_PACKAGE_DISTRIBUTIONS,
    pyinstaller_runtime_manifest,
)

executable_name = "averqel-backend.exe" if sys.platform == "win32" else "averqel-backend"
included_modules = {entry[0] for entry in a.pure}
runtime_manifest = pyinstaller_runtime_manifest(
    RUNTIME_PACKAGE_DISTRIBUTIONS,
    included_modules,
    executable_name=executable_name,
)
manifest_descriptor, manifest_path = tempfile.mkstemp(prefix="averqel-runtime-", suffix=".json")
with os.fdopen(manifest_descriptor, "w", encoding="utf-8") as manifest_stream:
    json.dump(runtime_manifest, manifest_stream, indent=2)
    manifest_stream.write("\n")
a.datas.append(("runtime.json", manifest_path, "DATA"))
atexit.register(lambda: Path(manifest_path).unlink(missing_ok=True))

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="averqel-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="averqel-backend",
)
