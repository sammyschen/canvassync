"""Walk Canvas modules, pages and assignments and mirror files into the destination folder."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath

from .client import AuthExpired, CanvasClient, CanvasError, Forbidden
from .config import Config, CourseEntry
from .manifest import Manifest

log = logging.getLogger("canvassync")

_VIDEO_PAT = re.compile(
    r"panopto|echo360|mediasite|kaltura|lecture ?capture|recording|youtube|youtu\.be|vimeo"
    r"|stream|video",
    re.IGNORECASE,
)

# file links inside page/assignment HTML: /courses/123/files/456, /files/456/download, …
_FILE_LINK = re.compile(r"(?:/courses/(\d+))?/files/(\d+)")
_HTML_URL = re.compile(r'(?:src|href)="([^"]+)"', re.IGNORECASE)
# images embedded in a page (banners, thumbnails) aren't course files worth syncing
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# names Windows refuses regardless of extension (CON.pdf is as invalid as CON)
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}

OTHER_FILES = "Other files"
ASSIGNMENTS = "Assignments"


def sanitize(name: str) -> str:
    s = _ILLEGAL_CHARS.sub("-", name)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s[:150] or "untitled"


def file_links(html: str | None, course_id: str) -> list[tuple[str, str]]:
    """(course_id, file_id) pairs linked from a Canvas HTML body, in order, deduplicated."""
    seen: dict[str, str] = {}
    for m in _FILE_LINK.finditer(_IMG_TAG.sub("", html or "")):
        seen.setdefault(m.group(2), m.group(1) or course_id)
    return [(cid, fid) for fid, cid in seen.items()]


def video_links(html: str | None) -> list[str]:
    seen: dict[str, None] = {}
    for url in _HTML_URL.findall(html or ""):
        if url.startswith("http") and _VIDEO_PAT.search(url):
            seen.setdefault(url.replace("&amp;", "&"), None)
    return list(seen)


@dataclass
class Stats:
    courses: int = 0
    downloaded: int = 0
    updated: int = 0
    skipped: int = 0
    links_added: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"{self.courses} courses: {self.downloaded} new, {self.updated} updated, "
            f"{self.skipped} unchanged, {self.links_added} video links, {self.errors} errors"
        )


def update_course_list(client: CanvasClient, config: Config) -> None:
    """Add newly discovered enrolments to the config (enabled by default)."""
    for course in client.courses():
        name = course.get("name")
        if not course.get("id") or not name:
            continue  # date-restricted courses come back without a name
        cid = str(course["id"])
        if cid not in config.courses:
            config.courses[cid] = CourseEntry(name=name)
            log.info("new course: %s", name)
        else:
            config.courses[cid].name = name
    config.save()


def run_sync(client: CanvasClient, config: Config, manifest: Manifest, *, full: bool = False) -> Stats:
    """Sync every enabled course.

    By default files already downloaded aren't looked up again and pages are
    only re-read when Canvas says they were edited; `full` re-checks everything.
    """
    stats = Stats()
    update_course_list(client, config)

    # path -> file id from earlier runs, so a new file can't overwrite an old one's path
    owners = {state["path"]: fid for fid, state in manifest._data["files"].items()}

    enabled = [(cid, entry) for cid, entry in config.courses.items() if entry.enabled]
    for i, (cid, entry) in enumerate(enabled, 1):
        log.info("[%d/%d] checking %s", i, len(enabled), entry.name)
        course_dir = config.dest / sanitize(entry.name)
        try:
            synced = _CourseSync(client, cid, entry.name, course_dir, manifest, stats, owners,
                                 full=full).run()
        except AuthExpired:
            raise
        except CanvasError as exc:
            log.error("failed to sync %s: %s", entry.name, exc)
            stats.errors += 1
            synced = False
        if synced:
            stats.courses += 1
        manifest.save()

    return stats


@dataclass
class _CourseSync:
    client: CanvasClient
    course_id: str
    course_name: str
    course_dir: Path
    manifest: Manifest
    stats: Stats
    owners: dict[str, str]
    full: bool = False
    # file id -> metadata from the bulk listing (empty if the Files tab is hidden)
    known: dict[str, dict] = field(default_factory=dict)
    handled: set[str] = field(default_factory=set)
    claimed: dict[str, str] = field(default_factory=dict)
    # module item id -> page updatedAt (empty if unavailable or on a full sync)
    page_updates: dict[str, str] = field(default_factory=dict)

    def run(self) -> bool:
        """Sync one course; returns False if nothing in it was accessible."""
        files = self._try("list files", self.client.files, self.course_id)
        folders = self._try("list folders", self.client.folders, self.course_id) if files else None
        modules = self._try("list modules", self.client.modules, self.course_id)
        assignments = self._try("list assignments", self.client.assignments, self.course_id)
        tabs = self._try("list tabs", self.client.tabs, self.course_id)
        if files is None and modules is None and assignments is None:
            log.warning("no access to %s (unavailable course?) — skipping", self.course_name)
            return False

        self.known = {str(f["id"]): f for f in files or []}
        has_pages = any(i.get("type") == "Page" for m in modules or [] for i in m.get("items") or [])
        if has_pages and not self.full:
            try:
                self.page_updates = self.client.page_updates(self.course_id)
            except AuthExpired:
                raise
            except CanvasError as exc:
                log.debug("page timestamps unavailable in %s, reading every page: %s",
                          self.course_name, exc)
        for module in modules or []:
            self._module(module)
        for assignment in assignments or []:
            self._assignment(assignment)
        for tab in tabs or []:
            self._tab(tab)
        self._other_files(folders or [])
        return True

    def _try(self, what: str, fn, *args):
        try:
            return fn(*args)
        except AuthExpired:
            raise
        except Forbidden as exc:
            log.debug("%s in %s: %s", what, self.course_name, exc)
        except CanvasError as exc:
            log.warning("cannot %s in %s: %s", what, self.course_name, exc)
            self.stats.errors += 1
        return None

    # -- walkers ------------------------------------------------------------

    def _module(self, module: dict) -> None:
        mod_dir = self.course_dir / sanitize(module.get("name") or "Module")
        cur_dir = mod_dir
        for item in module.get("items") or []:
            kind = item.get("type")
            title = item.get("title") or "Untitled"
            if kind == "SubHeader":
                cur_dir = mod_dir / sanitize(title)
            elif kind == "File" and item.get("content_id"):
                self._file(self.course_id, str(item["content_id"]), cur_dir)
            elif kind == "Page" and item.get("page_url"):
                self._page(item, title, cur_dir)
            elif kind in ("ExternalUrl", "ExternalTool"):
                ext = item.get("external_url") or ""
                url = ext if kind == "ExternalUrl" else (item.get("html_url") or ext)
                if url and _VIDEO_PAT.search(f"{title} {ext}"):
                    self._record_link(f"item:{item['id']}", url, title, cur_dir)

    def _assignment(self, assignment: dict) -> None:
        html = assignment.get("description")
        if not html:
            return
        title = assignment.get("name") or "Assignment"
        self._html(html, title, self.course_dir / ASSIGNMENTS / sanitize(title))

    def _tab(self, tab: dict) -> None:
        label = tab.get("label") or ""
        if tab.get("type") == "external" and _VIDEO_PAT.search(label):
            url = tab.get("full_url") or tab.get("html_url") or ""
            if url:
                self._record_link(f"tab:{tab['id']}", url, label, self.course_dir)

    def _page(self, item: dict, title: str, dir_path: Path) -> None:
        key = f"{self.course_id}:{item['page_url']}"
        updated = self.page_updates.get(str(item["id"]))
        cached = self.manifest.page(key)
        if updated and cached and cached.get("updated") == updated:
            files = [(cid, fid) for cid, fid in cached["files"]]
            videos = cached["videos"]
        else:
            page = self._try(f"open page '{title}'", self.client.page,
                             self.course_id, item["page_url"])
            if not page:
                return
            html = page.get("body")
            files, videos = file_links(html, self.course_id), video_links(html)
            if updated:
                self.manifest.record_page(key, updated, [list(f) for f in files], videos)
        self._links(files, videos, title, dir_path)

    def _html(self, html: str | None, title: str, dir_path: Path) -> None:
        self._links(file_links(html, self.course_id), video_links(html), title, dir_path)

    def _links(self, files: list[tuple[str, str]], videos: list[str], title: str,
               dir_path: Path) -> None:
        for cid, fid in files:
            self._file(cid, fid, dir_path)
        for i, url in enumerate(videos, 1):
            self._record_link(f"url:{url}", url, f"{title} (embedded video {i})", dir_path)

    def _other_files(self, folders: list[dict]) -> None:
        folder_paths = {str(f["id"]): f.get("full_name") or "" for f in folders}
        for fid, meta in self.known.items():
            if fid in self.handled:
                continue
            parts = PurePosixPath(folder_paths.get(str(meta.get("folder_id")), "")).parts
            if parts and parts[0] == "course files":
                parts = parts[1:]
            target_dir = self.course_dir / OTHER_FILES
            for part in parts:
                target_dir /= sanitize(part)
            self._file(self.course_id, fid, target_dir)

    # -- files ----------------------------------------------------------------

    def _file(self, course_id: str, file_id: str, dir_path: Path) -> None:
        if file_id in self.handled:
            return  # already placed elsewhere this run — first location wins
        self.handled.add(file_id)

        meta = self.known.get(file_id) if course_id == self.course_id else None
        state = self.manifest.file(file_id)
        if meta is None and not self.full and state and Path(state["path"]).exists():
            # already downloaded: Canvas gives a re-uploaded file a new id, so
            # there's nothing to gain from looking this one up again
            self.claimed[state["path"]] = file_id
            self.stats.skipped += 1
            return
        if meta is None:
            meta = self._try(f"look up file {file_id}", self.client.file, course_id, file_id)
            if meta is None:
                return
        name = meta.get("display_name") or meta.get("filename") or file_id
        # hidden_for_user files ("only available with link") are fine to download
        if meta.get("locked_for_user") or not meta.get("url"):
            log.debug("skipping locked file %s", name)
            return

        modified = meta.get("modified_at") or meta.get("updated_at")
        if state and state.get("modified") == modified and Path(state["path"]).exists():
            self.claimed[state["path"]] = file_id
            self.stats.skipped += 1
            return

        target = self._target(file_id, dir_path, sanitize(name))
        try:
            self.client.download(meta["url"], target)
        except AuthExpired:
            raise
        except CanvasError as exc:
            log.error("download failed for %s: %s", name, exc)
            self.stats.errors += 1
            return

        if state:
            self.stats.updated += 1
            log.info("updated  %s", target)
        else:
            self.stats.downloaded += 1
            log.info("download %s", target)
        self.manifest.record_file(file_id, str(target), modified)
        self.owners[str(target)] = file_id

    def _target(self, file_id: str, dir_path: Path, fname: str) -> Path:
        target = dir_path / fname
        key = str(target)
        taken_now = self.claimed.get(key) not in (None, file_id)
        owner = self.owners.get(key)
        taken_before = owner not in (None, file_id) and target.exists()
        if taken_now or taken_before:
            stem, dot, ext = fname.rpartition(".")
            fname = f"{stem} ({file_id}).{ext}" if dot and stem else f"{fname} ({file_id})"
            target = dir_path / fname
        self.claimed[str(target)] = file_id
        return target

    # -- video links ------------------------------------------------------------

    def _record_link(self, ref: str, url: str, title: str, dir_path: Path) -> None:
        key = f"{self.course_id}:{ref}"
        if self.manifest.has_link(key):
            return

        videos_md = self.course_dir / "videos.md"
        self.course_dir.mkdir(parents=True, exist_ok=True)
        if not videos_md.exists():
            videos_md.write_text(f"# Videos & recordings — {self.course_name}\n\n", encoding="utf-8")
        location = str(dir_path.relative_to(self.course_dir)) if dir_path != self.course_dir else ""
        suffix = f" (in {location})" if location and location != "." else ""
        with videos_md.open("a", encoding="utf-8") as f:
            f.write(f"- [{title}]({url}){suffix} — found {date.today().isoformat()}\n")
        self.manifest.record_link(key, url, title)
        self.stats.links_added += 1
        log.info("video link: %s (%s)", title, self.course_name)
