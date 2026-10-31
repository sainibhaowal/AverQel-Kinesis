from pathlib import Path

import huggingface_hub
import pytest

from app.core.averqel_home import get_averqel_home
from app.providers.api.providers import (
    InstallRerankerModelRequest,
    install_reranker_hub_model,
)

pytestmark = pytest.mark.unit_no_db


def test_reranker_install_uses_supported_huggingface_hub_options(
    monkeypatch, tmp_path: Path
) -> None:
    app_home = tmp_path / "averqel-home"
    monkeypatch.setenv("AVERQEL_HOME", str(app_home))
    get_averqel_home.cache_clear()
    downloaded: dict[str, object] = {}

    # Keep this fake's signature aligned with snapshot_download's supported
    # options. Removed options such as local_dir_use_symlinks/resume_download
    # will make the install call fail with TypeError, as they do on Hub 1.x.
    def fake_snapshot_download(
        repo_id: str,
        *,
        local_dir: str,
        ignore_patterns: list[str],
        max_workers: int = 8,
    ) -> str:
        downloaded.update(
            repo_id=repo_id,
            local_dir=local_dir,
            ignore_patterns=ignore_patterns,
            max_workers=max_workers,
        )
        target = Path(local_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.json").write_text("{}", encoding="utf-8")
        return str(target)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    try:
        response = install_reranker_hub_model(
            InstallRerankerModelRequest(model_id="cross-encoder/ms-marco-TinyBERT-L-2-v2")
        )
    finally:
        get_averqel_home.cache_clear()

    expected_path = (
        app_home
        / "models"
        / "rerankers"
        / "cross-encoder--ms-marco-TinyBERT-L-2-v2"
    )
    assert downloaded["repo_id"] == "cross-encoder/ms-marco-TinyBERT-L-2-v2"
    assert downloaded["local_dir"] == str(expected_path)
    assert response.status == "success"
    assert response.model.is_installed is True
    assert (expected_path / "averqel_model.json").is_file()
