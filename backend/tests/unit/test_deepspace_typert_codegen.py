from __future__ import annotations

from pathlib import Path

import pytest

from app.deepspace.typert.codegen import (
    check_artifacts,
    render_artifact_tree,
    write_artifacts,
)
from app.deepspace.typert.descriptors import RemoteContribution, RemoteDescriptor
from app.deepspace.typert.registry import TypertRegistry


def _registry() -> TypertRegistry:
    registry = TypertRegistry()
    descriptor = RemoteDescriptor(
        id="example/hello",
        service="example",
        namespace="example",
        method="hello",
        implementation=lambda: {"message": "hello"},
    )
    registry.register(RemoteContribution(package="example.package", descriptors=(descriptor,)))
    return registry


def test_python_artifacts_write_and_check_without_touching_unrelated_files(tmp_path: Path) -> None:
    artifacts = render_artifact_tree(_registry(), "example.package")
    client_source = next(item.content for item in artifacts if item.path.endswith("client.py"))
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    written = write_artifacts(artifacts, tmp_path)

    assert len(written) == 2
    assert "TypertRemoteClient" in client_source
    assert "self._client.invoke('example/hello', arguments)" in client_source
    assert all(path.is_file() for path in written)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert check_artifacts(artifacts, tmp_path) == []


def test_python_artifact_check_reports_missing_and_stale_paths(tmp_path: Path) -> None:
    artifacts = render_artifact_tree(_registry(), "example.package")
    write_artifacts(artifacts, tmp_path)
    (tmp_path / artifacts[0].path).write_text("stale", encoding="utf-8")
    (tmp_path / artifacts[1].path).unlink()

    problems = check_artifacts(artifacts, tmp_path)

    assert problems == [
        f"stale generated artifact: {artifacts[0].path}",
        f"missing generated artifact: {artifacts[1].path}",
    ]


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py"])
def test_python_artifact_writer_rejects_paths_outside_output_root(
    tmp_path: Path,
    path: str,
) -> None:
    from app.deepspace.typert.codegen import GeneratedArtifact

    with pytest.raises(ValueError, match="unsafe"):
        write_artifacts([GeneratedArtifact(path=path, content="no")], tmp_path)


def test_python_artifact_writer_rejects_symlink_escape(tmp_path: Path) -> None:
    from app.deepspace.typert.codegen import GeneratedArtifact

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    output = tmp_path / "out"
    output.mkdir()
    (output / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        write_artifacts(
            [GeneratedArtifact(path="escape/generated.py", content="no")],
            output,
        )
