"""Full-text search over the downloaded notes (SQLite FTS5 + per-page extraction)."""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .config import CANVASSYNC_DIR, Config

log = logging.getLogger("canvassync")

INDEX_PATH = CANVASSYNC_DIR / "index.db"

SUPPORTED = {".pdf", ".docx", ".pptx", ".ipynb"}

# snippet() highlight markers, replaced with ANSI/plain markers at render time
HL_START, HL_END = "[[", "]]"

# what a "page" means per file type, for display
PAGE_LABEL = {".pdf": "p.", ".pptx": "slide ", ".ipynb": "cell ", ".docx": ""}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    course TEXT NOT NULL,
    mtime REAL NOT NULL,
    page_count INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    content, doc_id UNINDEXED, page UNINDEXED, tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS summaries(
    path TEXT NOT NULL,
    page INTEGER NOT NULL,
    query TEXT NOT NULL,
    summary TEXT NOT NULL,
    PRIMARY KEY(path, page, query)
);
"""


# -- text extraction ---------------------------------------------------------

def _pdf_pages(path: Path) -> list[tuple[int, str]]:
    from pypdf import PdfReader

    out = []
    for i, page in enumerate(PdfReader(path).pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            out.append((i, text))
    return out


def _docx_pages(path: Path) -> list[tuple[int, str]]:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    text = html.unescape(" ".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S)))
    return [(1, text)] if text.strip() else []


def _pptx_pages(path: Path) -> list[tuple[int, str]]:
    out = []
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"\d+", n).group()),
        )
        for i, name in enumerate(slides, start=1):
            xml = z.read(name).decode("utf-8", "ignore")
            text = html.unescape(" ".join(re.findall(r"<a:t>(.*?)</a:t>", xml, re.S)))
            if text.strip():
                out.append((i, text))
    return out


def _ipynb_pages(path: Path) -> list[tuple[int, str]]:
    nb = json.loads(path.read_text(errors="ignore"))
    out = []
    for i, cell in enumerate(nb.get("cells", []), start=1):
        src = cell.get("source", [])
        text = "".join(src) if isinstance(src, list) else str(src)
        if text.strip():
            out.append((i, text))
    return out


_EXTRACTORS = {
    ".pdf": _pdf_pages,
    ".docx": _docx_pages,
    ".pptx": _pptx_pages,
    ".ipynb": _ipynb_pages,
}


def extract_pages(path: Path) -> list[tuple[int, str]]:
    extractor = _EXTRACTORS.get(path.suffix.lower())
    if not extractor:
        return []
    try:
        return extractor(path)
    except Exception as exc:
        log.warning("could not extract text from %s: %s", path.name, exc)
        return []


def _extract_worker(path_str: str) -> tuple[str, list[tuple[int, str]]]:
    return path_str, extract_pages(Path(path_str))


# -- indexing ----------------------------------------------------------------

def _open_db() -> sqlite3.Connection:
    CANVASSYNC_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(INDEX_PATH)
    con.executescript(_SCHEMA)
    cols = {row[1] for row in con.execute("PRAGMA table_info(summaries)")}
    if "query" not in cols:
        # summaries used to be keyed by (path, page) only — that cache is
        # invalid now that summaries are query-specific, so drop and recreate
        con.execute("DROP TABLE summaries")
        con.executescript(_SCHEMA)
        con.commit()
    return con


def update_index(config: Config, *, rebuild: bool = False) -> tuple[int, int]:
    """Index new/changed files under config.dest; returns (indexed, removed)."""
    con = _open_db()
    if rebuild:
        con.execute("DELETE FROM pages")
        con.execute("DELETE FROM docs")
        con.execute("DELETE FROM summaries")
        con.commit()

    dest = config.dest
    on_disk = {
        str(p.relative_to(dest)): p.stat().st_mtime
        for p in dest.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED
    }
    known = {
        path: (doc_id, mtime)
        for doc_id, path, mtime in con.execute("SELECT id, path, mtime FROM docs")
    }

    removed = [path for path in known if path not in on_disk]
    for path in removed:
        con.execute("DELETE FROM pages WHERE doc_id = ?", (known[path][0],))
        con.execute("DELETE FROM docs WHERE id = ?", (known[path][0],))
        con.execute("DELETE FROM summaries WHERE path = ?", (path,))

    todo = [
        path
        for path, mtime in on_disk.items()
        if path not in known or abs(known[path][1] - mtime) > 1e-6
    ]
    if todo:
        log.info("indexing %d file(s)…", len(todo))

    done = 0
    with ProcessPoolExecutor() as pool:
        for abs_path, extracted in pool.map(
            _extract_worker, (str(dest / t) for t in todo), chunksize=4
        ):
            rel = str(Path(abs_path).relative_to(dest))
            course = Path(rel).parts[0]
            if rel in known:
                doc_id = known[rel][0]
                con.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
                con.execute("DELETE FROM summaries WHERE path = ?", (rel,))  # content changed
                con.execute(
                    "UPDATE docs SET mtime = ?, page_count = ? WHERE id = ?",
                    (on_disk[rel], len(extracted), doc_id),
                )
            else:
                doc_id = con.execute(
                    "INSERT INTO docs(path, course, mtime, page_count) VALUES(?,?,?,?)",
                    (rel, course, on_disk[rel], len(extracted)),
                ).lastrowid
            con.executemany(
                "INSERT INTO pages(content, doc_id, page) VALUES(?,?,?)",
                [(text, doc_id, page) for page, text in extracted],
            )
            done += 1
            if done % 100 == 0:
                con.commit()
                log.info("  … %d/%d", done, len(todo))

    con.commit()
    con.close()
    return len(todo), len(removed)


# -- searching ---------------------------------------------------------------

def _fts_query(query: str) -> str:
    if '"' in query:
        return query  # user is writing raw FTS5 syntax (phrases, OR, NEAR, …)
    # quote each term so apostrophes etc. can't break the parser; terms AND together
    return " ".join(f'"{term}"' for term in query.split())


def index_exists() -> bool:
    return INDEX_PATH.exists()


def doc_count() -> int:
    con = _open_db()
    (n,) = con.execute("SELECT COUNT(*) FROM docs").fetchone()
    con.close()
    return n


def page_text(path: str, page: int) -> str | None:
    """Full extracted text of one page, straight from the index."""
    con = _open_db()
    row = con.execute(
        "SELECT pages.content FROM pages JOIN docs ON docs.id = pages.doc_id"
        " WHERE docs.path = ? AND pages.page = ?",
        (path, page),
    ).fetchone()
    con.close()
    return row[0] if row else None


def _norm_query(query: str) -> str:
    return " ".join(query.lower().split())


def get_summary(path: str, page: int, query: str) -> str | None:
    con = _open_db()
    row = con.execute(
        "SELECT summary FROM summaries WHERE path = ? AND page = ? AND query = ?",
        (path, page, _norm_query(query)),
    ).fetchone()
    con.close()
    return row[0] if row else None


def store_summary(path: str, page: int, query: str, summary: str) -> None:
    con = _open_db()
    con.execute(
        "INSERT OR REPLACE INTO summaries(path, page, query, summary) VALUES(?,?,?,?)",
        (path, page, _norm_query(query), summary),
    )
    con.commit()
    con.close()


def search(
    query: str, *, course: str | None = None, limit: int = 10, snippet_tokens: int = 12
) -> list[dict]:
    """Return up to `limit` documents, each with its best-matching pages."""
    con = _open_db()
    sql = f"""
        SELECT docs.path, docs.course, pages.page,
               snippet(pages, 0, '{HL_START}', '{HL_END}', ' … ', {int(snippet_tokens)}),
               bm25(pages)
        FROM pages JOIN docs ON docs.id = pages.doc_id
        WHERE pages MATCH ?
    """
    args: list = [_fts_query(query)]
    if course:
        sql += " AND docs.course LIKE ?"
        args.append(f"%{course}%")
    sql += " ORDER BY bm25(pages) LIMIT 120"
    rows = con.execute(sql, args).fetchall()
    con.close()

    grouped: dict[str, dict] = {}
    for path, course_name, page, snip, _rank in rows:
        doc = grouped.setdefault(path, {"path": path, "course": course_name, "hits": []})
        if len(doc["hits"]) < 3:
            doc["hits"].append((page, snip))
    return list(grouped.values())[:limit]
