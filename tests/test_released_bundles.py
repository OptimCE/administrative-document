"""A released bundle never changes: a regeneration must reach the same files.

A final version records the template row it rendered with, and that row names a
versioned prefix (``…/<doc_type>/v<n>/``). Regenerating the version from its
snapshot proves it is the file that was sent only if the files behind that
prefix are still the ones it was rendered from. The database freezes the row
(``trg_document_template_in_use``); this freezes the bytes.

``tests/fixtures/released_bundles.json`` holds a sha256 per file of every bundle
version. Changing a released file fails here: a revised form is a NEW version
folder (and a new ``document_template`` row), never an edit. Adding a version
fails too until it is added to the lock, which is the moment to decide it is
released. Text files are hashed with LF line endings, so a Windows checkout
(core.autocrlf) and CI agree.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLE_ROOT = _ROOT / "document-templates" / "administrative-document"
LOCK = _ROOT / "tests" / "fixtures" / "released_bundles.json"
_TEXT = {".json", ".html", ".css", ".md", ".txt"}


def _digest(path: pathlib.Path) -> str:
    data = path.read_bytes()
    if path.suffix in _TEXT:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def _on_disk() -> dict[str, dict[str, str]]:
    return {
        f"{version.parent.name}/{version.name}": {
            file.relative_to(version).as_posix(): _digest(file)
            for file in sorted(version.rglob("*"))
            if file.is_file()
        }
        for version in sorted(p for p in BUNDLE_ROOT.glob("*/v*") if p.is_dir())
    }


def _locked() -> dict[str, dict[str, str]]:
    locked: dict[str, dict[str, str]] = json.loads(LOCK.read_text(encoding="utf-8"))
    return locked


def test_no_released_file_changed_or_went_missing():
    on_disk = _on_disk()
    for version, files in _locked().items():
        assert version in on_disk, f"released bundle {version} was removed"
        assert (
            on_disk[version] == files
        ), f"released bundle {version} changed: publish a new version folder instead"


def test_every_bundle_version_is_locked():
    unlocked = sorted(set(_on_disk()) - set(_locked()))
    assert not unlocked, f"add these to {LOCK.name} once they are released: {unlocked}"
