# PyInstaller build spec for the complete local FastAPI runtime.
# This must be evaluated on the target OS and CPU architecture.
from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

backend_dir = Path(SPECPATH).resolve()

# FastAPI routes and provider implementations are partly imported by string, so
# include all app modules. MCP catalog seeding is also loaded dynamically.
hiddenimports = collect_submodules("app", on_error="warn")
hiddenimports += ["app.main", "scripts.seed_mcp_catalog"]
datas = collect_data_files("app")
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
    "faster_whisper",
    "kokoro_onnx",
    "mcp",
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
