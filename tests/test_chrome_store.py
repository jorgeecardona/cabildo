"""scripts/chrome_store.py: the zip for the Chrome Web Store, and the publish steps."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "chrome_store.py"


def load() -> Any:
    spec = importlib.util.spec_from_file_location("chrome_store", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


store = load()


class Fake:
    def __init__(self, status: dict[str, Any], upload: str = "SUCCEEDED") -> None:
        self.found = status
        self.state = upload
        self.calls: list[str] = []

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return {**self.found, "lastAsyncUploadState": "SUCCEEDED"}

    def upload(self, data: bytes) -> dict[str, Any]:
        self.calls.append("upload")
        return {"uploadState": self.state}

    def publish(self) -> dict[str, Any]:
        self.calls.append("publish")
        return {"state": "PENDING_REVIEW"}


def revision(version: str, state: str = "PUBLISHED") -> dict[str, Any]:
    return {"state": state, "distributionChannels": [{"crxVersion": version}]}


def test_the_zip_has_the_extension_without_its_key(tmp_path: Path) -> None:
    out = tmp_path / "x.zip"
    version = store.package(out=out)
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert "key" not in manifest and manifest["version"] == version
    named = {manifest["background"]["service_worker"], manifest["action"]["default_popup"]}
    assert named | set(manifest["icons"].values()) <= names


def test_a_version_the_store_has_is_skipped() -> None:
    fake = Fake({"publishedItemRevisionStatus": revision("0.0.1")})
    assert "already has" in store.release(fake, "0.0.1", b"zip")
    assert fake.calls == ["status"]


@pytest.mark.parametrize("first", ["SUCCEEDED", "IN_PROGRESS"])
def test_a_new_version_is_uploaded_and_submitted(first: str) -> None:
    fake = Fake({"publishedItemRevisionStatus": revision("0.0.1")}, first)
    assert "PENDING_REVIEW" in store.release(fake, "0.0.2", b"zip", wait=0)
    assert [c for c in fake.calls if c != "status"] == ["upload", "publish"]


@pytest.mark.parametrize(
    ("status", "upload"),
    [({"submittedItemRevisionStatus": revision("0.0.2", "PENDING_REVIEW")}, "SUCCEEDED"),
     ({}, "FAILED")],
)  # fmt: skip
def test_nothing_is_submitted_during_a_review_or_after_a_failed_upload(
    status: dict[str, Any], upload: str
) -> None:
    fake = Fake(status, upload)
    with pytest.raises(SystemExit):
        store.release(fake, "0.0.3", b"zip", wait=0)
    assert "publish" not in fake.calls


def test_without_credentials_it_only_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(store, "ZIP", tmp_path / "x.zip")
    assert store.main(["publish"], env={}) == 0
    assert "published nothing" in capsys.readouterr().out
    assert (tmp_path / "x.zip").is_file()
