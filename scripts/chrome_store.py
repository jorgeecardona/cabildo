# /// script
# requires-python = ">=3.12"
# dependencies = ["google-auth[requests]>=2.29"]
# ///
"""Build the Chrome extension's zip, and publish it to the Chrome Web Store.

``package`` writes ``dist/cabildo-chrome.zip``. ``publish`` builds it too, then uploads it and
submits it for review when these variables are set (see ``docs/chrome-web-store.md``):

- ``CWS_SERVICE_ACCOUNT``: path of the service account's JSON key file;
- ``CWS_PUBLISHER_ID`` and ``CWS_ITEM_ID``: from the developer dashboard.

The first version of an item can only be uploaded in the dashboard: the API has no "create".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "cabildo" / "browser" / "extension"
ZIP = ROOT / "dist" / "cabildo-chrome.zip"
API = "https://chromewebstore.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/chromewebstore"
_POLL_SECONDS = 5.0
_POLLS = 60


def package(source: Path = SOURCE, out: Path = ZIP) -> str:
    """Write the zip the store takes, and return the version in it.

    The manifest goes without its ``key``: the store refuses the field.
    """
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    del manifest["key"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                archive.write(path, path.relative_to(source).as_posix())
    return manifest["version"]


class Store(Protocol):
    """The three calls of the Chrome Web Store API that publishing needs."""

    def status(self) -> Mapping[str, Any]: ...
    def upload(self, data: bytes) -> Mapping[str, Any]: ...
    def publish(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class Api:
    """One item of one publisher, through the API (version 2)."""

    token: str
    publisher: str
    item: str

    def _call(self, url: str, data: bytes | None = None, post: bool = False) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/zip"
        request = urllib.request.Request(
            url, data=data, headers=headers, method="POST" if post else "GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as reply:
                return json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"the store answered {exc.code}: {exc.read().decode()}") from exc

    def _name(self) -> str:
        return f"publishers/{self.publisher}/items/{self.item}"

    def status(self) -> dict[str, Any]:
        return self._call(f"{API}/v2/{self._name()}:fetchStatus")

    def upload(self, data: bytes) -> dict[str, Any]:
        return self._call(f"{API}/upload/v2/{self._name()}:upload", data, post=True)

    def publish(self) -> dict[str, Any]:
        return self._call(f"{API}/v2/{self._name()}:publish", post=True)


def versions(revision: Mapping[str, Any]) -> set[str]:
    """The versions of one revision of ``fetchStatus``."""
    return {c.get("crxVersion", "") for c in revision.get("distributionChannels", [])}


def release(store: Store, version: str, data: bytes, wait: float = _POLL_SECONDS) -> str:
    """Upload ``data`` and submit it for review, and say what happened.

    Raises:
        SystemExit: another version is in review, the upload failed, or the store refused.
    """
    status = store.status()
    submitted = status.get("submittedItemRevisionStatus", {})
    published = status.get("publishedItemRevisionStatus", {})
    if version in versions(published) | versions(submitted):
        return f"the store already has {version}: published nothing."
    if submitted.get("state") == "PENDING_REVIEW":
        raise SystemExit(
            f"another version is in review, so {version} was not sent. Run this job again "
            "after the review."
        )
    state = store.upload(data).get("uploadState")
    for _ in range(_POLLS):
        if state != "IN_PROGRESS":
            break
        time.sleep(wait)
        state = store.status().get("lastAsyncUploadState")
    if state != "SUCCEEDED":
        raise SystemExit(f"the upload of {version} did not succeed: {state}")
    return f"submitted {version}: {store.publish().get('state')}"


def token(key_file: str) -> str:
    """An access token of the service account in ``key_file``."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    found = service_account.Credentials.from_service_account_file(key_file, scopes=[SCOPE])
    found.refresh(Request())
    return str(found.token)


def main(argv: list[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    """Run ``package`` or ``publish``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("package", "publish"))
    command = parser.parse_args(argv).command
    version = package(SOURCE, ZIP)
    print(f"built {ZIP} ({version})")
    if command == "package":
        return 0
    wanted = ("CWS_SERVICE_ACCOUNT", "CWS_PUBLISHER_ID", "CWS_ITEM_ID")
    missing = [name for name in wanted if not env.get(name)]
    if missing:
        print(f"{', '.join(missing)} not set: published nothing.")
        return 0
    key_file, publisher, item = (env[name] for name in wanted)
    print(release(Api(token(key_file), publisher, item), version, ZIP.read_bytes()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
