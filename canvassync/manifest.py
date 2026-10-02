"""Record of everything already downloaded, so syncs are idempotent."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import CANVASSYNC_DIR, MANIFEST_PATH


class Manifest:
    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls) -> "Manifest":
        if MANIFEST_PATH.exists():
            data = json.loads(MANIFEST_PATH.read_text())
        else:
            data = {}
        data.setdefault("files", {})
        data.setdefault("links", {})
        return cls(data)

    def save(self) -> None:
        CANVASSYNC_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(self._data, indent=2))

    # -- files ------------------------------------------------------------
    # Keyed by Canvas file id; `modified` is the file's updated_at, so a
    # re-uploaded file (same id, newer timestamp) is downloaded again.

    def file(self, file_id: str) -> dict | None:
        return self._data["files"].get(file_id)

    def record_file(self, file_id: str, path: str, modified: str | None) -> None:
        self._data["files"][file_id] = {
            "path": path,
            "modified": modified,
            "downloaded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    # -- video/external links -------------------------------------------

    def has_link(self, key: str) -> bool:
        return key in self._data["links"]

    def record_link(self, key: str, url: str, title: str) -> None:
        self._data["links"][key] = {"url": url, "title": title}

    # -- misc -------------------------------------------------------------

    @property
    def last_sync(self) -> str | None:
        return self._data.get("last_sync")

    def mark_synced(self) -> None:
        self._data["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
