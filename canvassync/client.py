"""Thin wrapper over Canvas's REST API, authenticated by browser cookies."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import BrowserContext

log = logging.getLogger("canvassync")

# polite delay between requests, seconds
_REQUEST_DELAY = 0.023

_HEADERS = {"Accept": "application/json"}

# Canvas prefixes cookie-authenticated JSON with this to defeat JSON hijacking
_JSON_GUARD = "while(1);"

_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class CanvasError(Exception):
    pass


class AuthExpired(CanvasError):
    pass


class Forbidden(CanvasError):
    pass


def parse_json(text: str):
    if text.startswith(_JSON_GUARD):
        text = text[len(_JSON_GUARD):]
    return json.loads(text)


def next_page(link_header: str | None) -> str | None:
    """Extract the rel="next" URL from a Canvas pagination Link header."""
    if not link_header:
        return None
    m = _NEXT_LINK.search(link_header)
    return m.group(1) if m else None


class CanvasClient:
    def __init__(self, ctx: BrowserContext, base_url: str):
        self._req = ctx.request
        self.base_url = base_url

    def _url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else self.base_url + path

    def _get(self, path: str, *, headers: dict | None = None):
        time.sleep(_REQUEST_DELAY)
        resp = self._req.get(self._url(path), headers=headers)
        if resp.status == 401:
            # Canvas also uses 401 for "unauthorized for this resource" (e.g. a
            # hidden Files tab); only a dead session fails users/self too.
            body = resp.text()
            if "user authorization required" in body.lower() or "/api/v1/users/self" in path:
                raise AuthExpired("Canvas session rejected (401)")
            raise Forbidden(f"access denied: {path}")
        if resp.status in (403, 404):
            raise Forbidden(f"access denied: {path} ({resp.status})")
        if not resp.ok:
            raise CanvasError(f"GET {path} -> {resp.status}")
        return resp

    def _json(self, path: str):
        return parse_json(self._get(path, headers=_HEADERS).text())

    def _paged(self, path: str) -> list[dict]:
        sep = "&" if "?" in path else "?"
        url: str | None = f"{path}{sep}per_page=100"
        results: list[dict] = []
        while url:
            resp = self._get(url, headers=_HEADERS)
            data = parse_json(resp.text())
            if isinstance(data, list):
                results.extend(data)
            url = next_page(resp.headers.get("link"))
        return results

    def courses(self) -> list[dict]:
        return self._paged("/api/v1/users/self/courses?include[]=term")

    def modules(self, course_id: str) -> list[dict]:
        modules = self._paged(f"/api/v1/courses/{course_id}/modules?include[]=items")
        for mod in modules:
            # Canvas omits inline items for very large modules
            if "items" not in mod:
                mod["items"] = self._paged(
                    f"/api/v1/courses/{course_id}/modules/{mod['id']}/items"
                )
        return modules

    def file(self, course_id: str, file_id: str) -> dict:
        return self._json(f"/api/v1/courses/{course_id}/files/{file_id}")

    def files(self, course_id: str) -> list[dict]:
        return self._paged(f"/api/v1/courses/{course_id}/files")

    def folders(self, course_id: str) -> list[dict]:
        return self._paged(f"/api/v1/courses/{course_id}/folders")

    def page(self, course_id: str, page_url: str) -> dict:
        return self._json(f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe='')}")

    def assignments(self, course_id: str) -> list[dict]:
        return self._paged(f"/api/v1/courses/{course_id}/assignments")

    def tabs(self, course_id: str) -> list[dict]:
        return self._paged(f"/api/v1/courses/{course_id}/tabs")

    def download(self, url: str, dest: Path) -> None:
        resp = self._get(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(resp.body())
        part.replace(dest)
