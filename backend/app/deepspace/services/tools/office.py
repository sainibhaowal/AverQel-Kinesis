"""Workspace-scoped Office conversion, recalculation, and rendering tools."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from app.deepspace.services.runtime_manifest import RuntimeManifestError
from app.deepspace.services.subprocess_stdio import (
    SubprocessCollect,
    SubprocessStdio,
    spawn_with_stdio,
)
from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import (
    NativeToolSpec,
    ToolContext,
    define_native_tool,
    string_property,
)
from app.deepspace.services.tools.dependencies import workspace_dependencies
from app.deepspace.services.tools.fs import _resolve, _root_for
from app.deepspace.services.workspace_root import WorkspacePathError, resolve_within_root

MAX_OFFICE_INPUT_BYTES = 100_000_000
MAX_RENDER_PAGES = 30
OFFICE_TIMEOUT_SECONDS = 180
_OFFICE_EXTENSIONS = frozenset(
    {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp", ".pdf"}
)


class OfficeProcessArguments(ToolArguments):
    """Arguments for one bounded, workspace-local Office operation."""

    operation: Literal["export_pdf", "recalculate_workbook", "render_pages"]
    input_path: str = Field(min_length=1, max_length=1_024)
    output_path: str | None = Field(default=None, max_length=1_024)
    overwrite: bool = False
    page_limit: int = Field(default=10, ge=1, le=MAX_RENDER_PAGES)


def _binary_paths(settings: Any) -> tuple[str | None, str | None]:
    dependencies = workspace_dependencies(settings)
    office = dependencies.get("office_runtime") or {}
    return office.get("soffice") or office.get("libreoffice"), office.get("pdftoppm")


def _output_path(
    context: ToolContext,
    raw_path: str,
    *,
    must_not_exist: bool,
    allow_existing_directory: bool = False,
) -> tuple[Path | None, str | None]:
    root = _root_for(context)
    mode = str(
        context.sandbox_mode
        or getattr(context.settings, "deepspace_permission_mode", "workspace-write")
    )
    try:
        del mode  # Office outputs are always confined to the active workspace.
        resolved = resolve_within_root(root, raw_path)
    except WorkspacePathError as exc:
        return None, str(exc)
    if must_not_exist and resolved.absolute.exists():
        return None, "The output already exists; set overwrite=true to replace it."
    if resolved.absolute.is_dir() and not allow_existing_directory:
        return None, "The output path must name a file, not a directory."
    return resolved.absolute, None


def _atomic_copy(source: Path, destination: Path, *, overwrite: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
            shutil.copyfileobj(original, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if overwrite:
            os.replace(temporary_name, destination)
        else:
            os.link(temporary_name, destination)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


async def _run_office(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str, str]:
    handle = await spawn_with_stdio(
        argv,
        cwd=str(cwd),
        env={"HOME": str(cwd), "TMPDIR": str(cwd), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        stdio=SubprocessStdio(
            stdin="ignore",
            stdout=SubprocessCollect(max_bytes=32_000),
            stderr=SubprocessCollect(max_bytes=32_000),
        ),
    )
    try:
        result = await asyncio.wait_for(handle.wait(), timeout=timeout)
    except TimeoutError:
        await handle.terminate_and_wait()
        return -1, "", "The Office process exceeded its time limit."
    stdout = handle.collect_final("stdout") or {}
    stderr = handle.collect_final("stderr") or {}
    return int(result.exit_code or 0), str(stdout.get("text") or ""), str(stderr.get("text") or "")


async def _office_process(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """Run an Office task on a private copy and write outputs in the workspace."""
    resolved, refusal = _resolve(context, str(arguments["input_path"]))
    if refusal is not None:
        return refusal
    assert resolved is not None
    source = resolved.absolute
    if not source.is_file():
        return {
            "status": "not_found",
            "path": resolved.relative,
            "message": "The Office input file is unavailable.",
        }
    if source.suffix.casefold() not in _OFFICE_EXTENSIONS:
        return {
            "status": "unsupported",
            "message": "This Office operation does not support that file type.",
        }
    if source.stat().st_size > MAX_OFFICE_INPUT_BYTES:
        return {
            "status": "refused",
            "message": "The Office input exceeds the 100 MB processing limit.",
        }
    operation = str(arguments["operation"])
    direct_pdf_render = operation == "render_pages" and source.suffix.casefold() == ".pdf"
    if source.suffix.casefold() == ".pdf" and operation != "render_pages":
        return {
            "status": "unsupported",
            "message": "PDF input is supported only by the render_pages operation.",
        }
    try:
        office_binary, pdf_renderer = _binary_paths(context.settings)
    except RuntimeManifestError as exc:
        return {
            "status": "unavailable",
            "error_code": "RUNTIME_MANIFEST_INVALID",
            "message": str(exc),
        }
    if not office_binary and not direct_pdf_render:
        return {
            "status": "unavailable",
            "capability_failure": True,
            "retryable": False,
            "message": "LibreOffice is not present in this runtime. Install or package it, then retry.",
        }

    overwrite = bool(arguments.get("overwrite", False))
    root = _root_for(context)
    raw_output = str(arguments.get("output_path") or "").strip()
    if operation == "render_pages" and not pdf_renderer:
        return {"status": "unavailable", "message": "pdftoppm is not present in this runtime."}
    if operation == "recalculate_workbook" and source.suffix.casefold() not in {".xlsx", ".ods"}:
        return {
            "status": "unsupported",
            "message": "Recalculation supports .xlsx and .ods workbooks.",
        }

    if operation == "render_pages":
        default_output = str(Path(resolved.relative).with_suffix("")) + "_pages"
        output, error = _output_path(
            context,
            raw_output or default_output,
            must_not_exist=not overwrite,
            allow_existing_directory=overwrite,
        )
        if error:
            return {"status": "refused", "message": error}
        assert output is not None
        if output.exists() and overwrite:
            return {
                "status": "refused",
                "message": "An existing render output is not replaced automatically.",
            }
    else:
        if raw_output:
            output_name = raw_output
        elif operation == "export_pdf":
            output_name = str(Path(resolved.relative).with_suffix(".pdf"))
        else:
            output_name = str(
                Path(resolved.relative).with_name(
                    f"{Path(resolved.relative).stem}_recalculated{source.suffix}"
                )
            )
        output, error = _output_path(context, output_name, must_not_exist=not overwrite)
        if error:
            return {"status": "refused", "message": error}
        assert output is not None
        if output == source:
            return {
                "status": "refused",
                "message": "The Office operation never overwrites its input file.",
            }
        expected_suffix = ".pdf" if operation == "export_pdf" else source.suffix.casefold()
        if output.suffix.casefold() != expected_suffix:
            return {
                "status": "refused",
                "message": f"The output file must use the {expected_suffix} extension.",
            }

    try:
        with tempfile.TemporaryDirectory(prefix="deepspace-office-") as temporary:
            work = Path(temporary)
            isolated_home = work / "home"
            isolated_home.mkdir()
            input_copy = work / source.name
            shutil.copy2(source, input_copy)
            profile_uri = (work / "profile").as_uri()
            if operation in {"export_pdf", "render_pages"}:
                outdir = work / "converted"
                outdir.mkdir()
                if direct_pdf_render:
                    generated_pdf = input_copy
                else:
                    assert office_binary is not None
                    code, _, stderr = await _run_office(
                        [
                            office_binary,
                            "--headless",
                            "--nologo",
                            "--nodefault",
                            "--nofirststartwizard",
                            "--norestore",
                            f"-env:UserInstallation={profile_uri}",
                            "--convert-to",
                            "pdf",
                            "--outdir",
                            str(outdir),
                            str(input_copy),
                        ],
                        cwd=work,
                        timeout=OFFICE_TIMEOUT_SECONDS,
                    )
                    generated_pdf = outdir / f"{input_copy.stem}.pdf"
                    if (
                        code != 0
                        or not generated_pdf.is_file()
                        or generated_pdf.stat().st_size == 0
                    ):
                        return {
                            "status": "error",
                            "message": "LibreOffice did not produce a valid PDF.",
                            "detail": stderr[-2_000:],
                        }
                if operation == "export_pdf":
                    _atomic_copy(generated_pdf, output, overwrite=overwrite)
                    return {
                        "status": "ok",
                        "operation": operation,
                        "input_path": resolved.relative,
                        "output_path": output.relative_to(root).as_posix(),
                        "size_bytes": output.stat().st_size,
                    }
                page_dir = work / "pages"
                page_dir.mkdir()
                assert pdf_renderer is not None
                code, _, stderr = await _run_office(
                    [
                        pdf_renderer,
                        "-png",
                        "-f",
                        "1",
                        "-l",
                        str(int(arguments.get("page_limit", 10))),
                        str(generated_pdf),
                        str(page_dir / "page"),
                    ],
                    cwd=work,
                    timeout=OFFICE_TIMEOUT_SECONDS,
                )
                pages = sorted(page_dir.glob("page-*.png"))
                if code != 0 or not pages:
                    return {
                        "status": "error",
                        "message": "PDF rendering did not produce page images.",
                        "detail": stderr[-2_000:],
                    }
                output.parent.mkdir(parents=True, exist_ok=True)
                staged_output = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
                outputs: list[str] = []
                try:
                    for page in pages[: int(arguments.get("page_limit", 10))]:
                        target = staged_output / page.name
                        shutil.copy2(page, target)
                    os.replace(staged_output, output)
                except Exception:
                    shutil.rmtree(staged_output, ignore_errors=True)
                    raise
                outputs.extend(
                    (output / page.name).relative_to(root).as_posix()
                    for page in pages[: int(arguments.get("page_limit", 10))]
                )
                return {
                    "status": "ok",
                    "operation": operation,
                    "input_path": resolved.relative,
                    "output_directory": output.relative_to(root).as_posix(),
                    "pages": outputs,
                }

            outdir = work / "recalculated"
            outdir.mkdir()
            assert office_binary is not None
            code, _, stderr = await _run_office(
                [
                    office_binary,
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nofirststartwizard",
                    "--norestore",
                    f"-env:UserInstallation={profile_uri}",
                    "--convert-to",
                    source.suffix.lstrip("."),
                    "--outdir",
                    str(outdir),
                    str(input_copy),
                ],
                cwd=work,
                timeout=OFFICE_TIMEOUT_SECONDS,
            )
            recalculated = outdir / input_copy.name
            if code != 0 or not recalculated.is_file() or recalculated.stat().st_size == 0:
                return {
                    "status": "error",
                    "message": "LibreOffice did not produce a recalculated workbook.",
                    "detail": stderr[-2_000:],
                }
            _atomic_copy(recalculated, output, overwrite=overwrite)
            return {
                "status": "ok",
                "operation": operation,
                "input_path": resolved.relative,
                "output_path": output.relative_to(root).as_posix(),
                "size_bytes": output.stat().st_size,
            }
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "error", "message": f"Office processing failed: {exc}"}


OFFICE_PROCESS_TOOL: NativeToolSpec = define_native_tool(
    name="office_process",
    description=(
        "Export a workspace Office file to PDF, recalculate an XLSX/ODS workbook, or render a bounded number "
        "of pages to PNG using the runtime's packaged LibreOffice and pdftoppm. Inputs are copied first; outputs "
        "stay in the active workspace and never replace the input file."
    ),
    properties={
        "operation": {
            "type": "string",
            "enum": ["export_pdf", "recalculate_workbook", "render_pages"],
        },
        "input_path": string_property(
            "Office input path relative to the active workspace.", min_length=1, max_length=1_024
        ),
        "output_path": string_property(
            "Optional output file or render directory path relative to the workspace.",
            max_length=1_024,
        ),
        "overwrite": {
            "type": "boolean",
            "description": "Allow replacing an existing output file (never the input).",
        },
        "page_limit": {"type": "integer", "minimum": 1, "maximum": MAX_RENDER_PAGES},
    },
    required=["operation", "input_path"],
    arguments_model=OfficeProcessArguments,
    mode="write",
    handler=_office_process,
    deadline_seconds=OFFICE_TIMEOUT_SECONDS * 2,
)
OFFICE_TOOLS = [OFFICE_PROCESS_TOOL]

__all__ = ["OFFICE_PROCESS_TOOL", "OFFICE_TOOLS", "OfficeProcessArguments"]
