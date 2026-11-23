import base64
import shutil
import sys
from types import SimpleNamespace

import pytest

from app.deepspace.services.chat_service import NATIVE_TOOL_REGISTRY
from app.deepspace.services.sandbox_backend import BwrapBackend, SandboxExecBackend
from app.deepspace.services.sandbox_executor import SandboxExecutorError, execute_sandbox


def test_advanced_native_tools_are_provider_neutral() -> None:
    names = NATIVE_TOOL_REGISTRY.names()
    assert {"sandbox_execute", "read", "analyze"} <= names
    assert not {"document_read", "document_query", "document_compare"} & names


@pytest.mark.asyncio
async def test_sandbox_is_disabled_by_default() -> None:
    settings = SimpleNamespace(deepspace_sandbox_enabled=False)
    with pytest.raises(SandboxExecutorError, match="not enabled"):
        await execute_sandbox(code="print(1)", language="python", settings=settings)


@pytest.mark.asyncio
async def test_sandbox_rejects_unsupported_language_before_network() -> None:
    settings = SimpleNamespace(
        deepspace_sandbox_enabled=True, deepspace_sandbox_url="http://invalid"
    )
    with pytest.raises(SandboxExecutorError, match="Only Python"):
        await execute_sandbox(code="select 1", language="javascript", settings=settings)


@pytest.mark.asyncio
async def test_sandbox_rejects_oversized_library_bundle_before_network() -> None:
    settings = SimpleNamespace(
        deepspace_sandbox_enabled=True,
        deepspace_sandbox_url="http://invalid",
    )
    with pytest.raises(SandboxExecutorError, match="10 MB"):
        await execute_sandbox(
            code="print(1)",
            language="python",
            settings=settings,
            files=[
                {
                    "name": "large.csv",
                    "data_base64": base64.b64encode(b"x" * (11 * 1024 * 1024)).decode(),
                }
            ],
        )


@pytest.mark.asyncio
async def test_local_sandbox_executes_python_and_collects_generated_file(tmp_path) -> None:
    if sys.platform.startswith("linux") and (
        not BwrapBackend().available() or not shutil.which("prlimit")
    ):
        pytest.skip("Local sandbox integration requires usable bubblewrap and prlimit.")
    if sys.platform == "darwin" and not SandboxExecBackend().available():
        pytest.skip("Local sandbox integration requires usable macOS sandbox-exec.")
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        pytest.skip("No full-filesystem local sandbox is supported on this test platform.")
    settings = SimpleNamespace(
        deepspace_sandbox_enabled=True,
        deepspace_sandbox_url=None,
        deepspace_sandbox_backend="auto",
        deepspace_sandbox_network=True,
    )
    result = await execute_sandbox(
        code="from pathlib import Path\nPath('answer.txt').write_text('42')\nprint('done')",
        language="python",
        settings=settings,
        timeout_seconds=5,
    )
    assert result["status"] == "completed"
    assert result["stdout"].strip() == "done"
    assert result["sandbox_backend"] in {"bwrap", "sandbox-exec"}
    assert result["files"][0]["name"] == "answer.txt"
    assert base64.b64decode(result["files"][0]["data_base64"]) == b"42"


@pytest.mark.asyncio
async def test_local_sandbox_sql_reads_input_tables_and_rejects_mutation() -> None:
    if sys.platform.startswith("linux") and (
        not BwrapBackend().available() or not shutil.which("prlimit")
    ):
        pytest.skip("Local sandbox integration requires usable bubblewrap and prlimit.")
    if sys.platform == "darwin" and not SandboxExecBackend().available():
        pytest.skip("Local sandbox integration requires usable macOS sandbox-exec.")
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        pytest.skip("No full-filesystem local sandbox is supported on this test platform.")
    settings = SimpleNamespace(
        deepspace_sandbox_enabled=True,
        deepspace_sandbox_url=None,
        deepspace_sandbox_backend="auto",
        deepspace_sandbox_network=True,
    )
    result = await execute_sandbox(
        code="SELECT name FROM people",
        language="sql",
        settings=settings,
        input_data={"people": [{"name": "Ada"}]},
        timeout_seconds=5,
    )
    assert result["status"] == "completed"
    assert '"Ada"' in result["stdout"]
    mutation = await execute_sandbox(
        code="DELETE FROM people",
        language="sql",
        settings=settings,
        input_data={"people": [{"name": "Ada"}]},
        timeout_seconds=5,
    )
    assert mutation["status"] == "failed"
