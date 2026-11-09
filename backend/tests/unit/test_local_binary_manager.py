from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.local_vector.binaries import LocalBinaryManager

pytestmark = pytest.mark.unit_no_db


def test_binary_download_rejects_untrusted_schemes_before_io(tmp_path) -> None:
    manager = LocalBinaryManager(tmp_path)

    with pytest.raises(ValueError, match="approved HTTPS host"):
        manager._download_file("file:///etc/passwd", tmp_path / "artifact")

    assert not (tmp_path / "artifact").exists()


def test_libreoffice_appimage_download_targets_its_cache_file(tmp_path, monkeypatch) -> None:
    manager = LocalBinaryManager(tmp_path)
    downloads: list[tuple[str, Path]] = []

    def record_download(url: str, destination: Path) -> None:
        downloads.append((url, destination))
        destination.write_bytes(b"test binary")

    monkeypatch.setattr(manager, "_download_file", record_download)
    monkeypatch.setattr(
        "app.local_vector.binaries.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"LibreOffice test"),
    )

    result = manager._download_libreoffice_appimage()

    assert downloads[0][0].startswith("https://github.com/")
    assert downloads[0][1] == result
    assert result.read_bytes() == b"test binary"
