"""Tests for the browse directory picker backend.

The browse backend powers the in-app directory chooser.  The checks cover the
listing shape, hidden entries, the truncation bound, directory creation, and
the typed errors the host routes translate into the API error envelope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.deepspace.services.host.directory_picker.browse import (
    BrowseConfig,
    BrowseDirectoryPicker,
)
from app.deepspace.services.host.directory_picker.seam import (
    DirectoryPickerError,
    DirectoryPickerErrorCode,
)

pytestmark = pytest.mark.unit_no_db


def _list(picker: BrowseDirectoryPicker, path: str | None = None):
    return asyncio.run(picker.list(path))


def _create(picker: BrowseDirectoryPicker, path: str, name: str):
    return asyncio.run(picker.create_directory(path, name))


def test_the_listing_contains_only_directories_and_a_root_crumb(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "file.txt").write_text("not a directory")

    listing = _list(BrowseDirectoryPicker(), str(tmp_path))

    assert {Path(entry.path).name for entry in listing.entries} == {"alpha", "beta"}
    assert all(entry.hidden is False for entry in listing.entries)
    assert listing.crumbs[0].path == "/"
    assert listing.crumbs[-1].path == str(tmp_path)
    assert listing.home == str(Path.home())
    assert listing.truncated is False


def test_a_dot_prefixed_directory_is_reported_as_hidden(tmp_path: Path) -> None:
    (tmp_path / ".config").mkdir()

    listing = _list(BrowseDirectoryPicker(), str(tmp_path))

    assert [(Path(entry.path).name, entry.hidden) for entry in listing.entries] == [
        (".config", True)
    ]


def test_the_listing_is_truncated_at_the_configured_bound(tmp_path: Path) -> None:
    for name in ("alpha", "beta", "gamma"):
        (tmp_path / name).mkdir()

    listing = _list(BrowseDirectoryPicker(BrowseConfig(max_entries=2)), str(tmp_path))

    assert len(listing.entries) == 2
    assert listing.truncated is True


def test_an_empty_path_lists_the_home_directory() -> None:
    listing = _list(BrowseDirectoryPicker())

    assert listing.crumbs[-1].path == str(Path.home())


def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DirectoryPickerError) as caught:
        _list(BrowseDirectoryPicker(), "relative/path")

    assert caught.value.code is DirectoryPickerErrorCode.DIRECTORY_UNREADABLE


def test_a_missing_directory_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(DirectoryPickerError) as caught:
        _list(BrowseDirectoryPicker(), str(tmp_path / "missing"))

    assert caught.value.code is DirectoryPickerErrorCode.DIRECTORY_UNREADABLE


def test_create_makes_one_child_directory(tmp_path: Path) -> None:
    entry = _create(BrowseDirectoryPicker(), str(tmp_path), "new-project")

    assert entry.path == str(tmp_path / "new-project")
    assert (tmp_path / "new-project").is_dir()
    assert entry.hidden is False


def test_create_reports_an_existing_directory(tmp_path: Path) -> None:
    (tmp_path / "existing").mkdir()

    with pytest.raises(DirectoryPickerError) as caught:
        _create(BrowseDirectoryPicker(), str(tmp_path), "existing")

    assert caught.value.code is DirectoryPickerErrorCode.DIRECTORY_EXISTS


@pytest.mark.parametrize("name", ["", "..", ".", "two/parts", "back\\slash", " trailing"])
def test_create_refuses_an_invalid_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(DirectoryPickerError) as caught:
        _create(BrowseDirectoryPicker(), str(tmp_path), name)

    assert caught.value.code is DirectoryPickerErrorCode.DIRECTORY_CREATE_FAILED


def test_create_reports_a_missing_parent(tmp_path: Path) -> None:
    with pytest.raises(DirectoryPickerError) as caught:
        _create(BrowseDirectoryPicker(), str(tmp_path / "missing"), "child")

    assert caught.value.code is DirectoryPickerErrorCode.DIRECTORY_CREATE_FAILED
