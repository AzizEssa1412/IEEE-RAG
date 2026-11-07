"""SQLite registry: universities, documents (sources), ingestion runs, errors, courses.

ChromaDB holds the searchable passages; SQLite is the source of truth for WHAT was
ingested, from WHERE, WHEN, and whether it succeeded.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import COLLECTION_ALQUDS_UPLOADS, COLLECTION_ALQUDS_WEB, settings

_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS universities (
    id TEXT PRIMARY KEY,
    name_ar TEXT NOT NULL,
    name_en TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]',
    allowed_domains TEXT NOT NULL DEFAULT '[]',
    web_collection TEXT NOT NULL DEFAULT '',
    uploads_collection TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    university_id TEXT NOT NULL REFERENCES universities(id),
    collection TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT,
    file_ref TEXT,
    local_path TEXT,
    origin TEXT NOT NULL DEFAULT 'admin',
    parent_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    content_hash TEXT,
    version TEXT NOT NULL DEFAULT '',
    academic_year TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    extra TEXT NOT NULL DEFAULT '{}',
    retrieved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(university_id, collection, url)
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    university_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ingestion_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    document_id INTEGER,
    url TEXT,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    university_id TEXT NOT NULL,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    credits TEXT NOT NULL DEFAULT '',
    credits_theory TEXT NOT NULL DEFAULT '',
    credits_lab TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    program TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    semester TEXT NOT NULL DEFAULT '',
    requisites_raw TEXT NOT NULL DEFAULT '',
    page INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    description_page INTEGER NOT NULL DEFAULT 0,
    UNIQUE(document_id, code, category)
);
CREATE TABLE IF NOT EXISTS course_requisites (
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    required_code TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    university_id TEXT NOT NULL,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    faculty TEXT NOT NULL DEFAULT '',
    campus TEXT NOT NULL DEFAULT '',
    degree TEXT NOT NULL DEFAULT '',
    min_average TEXT NOT NULL DEFAULT '',
    streams TEXT NOT NULL DEFAULT '',
    academic_year TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS boilerplate (
    university_id TEXT NOT NULL,
    hash TEXT NOT NULL,
    sample TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (university_id, hash)
);
"""
# documents.status: pending | indexed | failed | duplicate | skipped
# documents.source_type: website | pdf | dataset | text
# documents.origin: seed | discovered | admin | upload
# ingestion_errors.stage: robots | fetch | extract | pdf | index | import
# course_requisites.kind: prerequisite | corequisite | condition


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_CONN: sqlite3.Connection | None = None


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(courses)")}
    for col, ddl in (("description", "TEXT NOT NULL DEFAULT ''"),
                     ("description_page", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            conn.execute(f"ALTER TABLE courses ADD COLUMN {col} {ddl}")
    conn.commit()


def get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.sqlite_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        seed_universities(conn)
        _CONN = conn
    return _CONN


def reset_connection() -> None:
    """Used by tests that point the data directory at a temp folder."""
    global _CONN
    if _CONN is not None:
        _CONN.close()
    _CONN = None


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _LOCK:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# --- Universities -------------------------------------------------------------------
ALQUDS = {
    "id": "alquds",
    "name_ar": "جامعة القدس",
    "name_en": "Al-Quds University",
    "aliases": ["جامعة القدس", "جامعه القدس", "al-quds", "alquds", "al quds", "aqu",
                "أبو ديس", "ابو ديس"],
    "allowed_domains": ["www.alquds.edu", "alquds.edu"],
    "web_collection": COLLECTION_ALQUDS_WEB,
    "uploads_collection": COLLECTION_ALQUDS_UPLOADS,
}


def seed_universities(conn: sqlite3.Connection) -> None:
    u = ALQUDS
    conn.execute(
        """INSERT OR IGNORE INTO universities
           (id, name_ar, name_en, aliases, allowed_domains, web_collection, uploads_collection,
            created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (u["id"], u["name_ar"], u["name_en"], json.dumps(u["aliases"], ensure_ascii=False),
         json.dumps(u["allowed_domains"]), u["web_collection"], u["uploads_collection"],
         now_iso()),
    )
    conn.commit()


def list_universities() -> list[dict[str, Any]]:
    rows = query("SELECT * FROM universities ORDER BY created_at")
    for r in rows:
        r["aliases"] = json.loads(r["aliases"])
        r["allowed_domains"] = json.loads(r["allowed_domains"])
    return rows


def get_university(uid: str) -> dict[str, Any] | None:
    return next((u for u in list_universities() if u["id"] == uid), None)


def add_university(uid: str, name_ar: str, name_en: str, aliases: list[str],
                   domains: list[str]) -> dict[str, Any]:
    with tx() as c:
        c.execute(
            """INSERT INTO universities (id, name_ar, name_en, aliases, allowed_domains,
               web_collection, uploads_collection, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (uid, name_ar, name_en, json.dumps(aliases, ensure_ascii=False),
             json.dumps(domains), f"{uid}_official_website",
             f"{uid}_uploaded_academic_documents", now_iso()),
        )
    return get_university(uid)  # type: ignore[return-value]


# --- Documents ----------------------------------------------------------------------
def upsert_document(**fields: Any) -> int:
    """Insert a document row, or return the existing one for the same university/collection/url."""
    ts = now_iso()
    with tx() as c:
        if fields.get("url"):
            row = c.execute(
                "SELECT id FROM documents WHERE university_id=? AND collection=? AND url=?",
                (fields["university_id"], fields["collection"], fields["url"]),
            ).fetchone()
            if row:
                return int(row["id"])
        cols = list(fields) + ["created_at", "updated_at"]
        vals = list(fields.values()) + [ts, ts]
        cur = c.execute(
            f"INSERT INTO documents ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals,
        )
        return int(cur.lastrowid)


def update_document(doc_id: int, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE documents SET {sets} WHERE id=?", (*fields.values(), doc_id))


def get_document(doc_id: int) -> dict[str, Any] | None:
    return query_one("SELECT * FROM documents WHERE id=?", (doc_id,))


def find_duplicate(content_hash: str, university_id: str, exclude_id: int) -> dict | None:
    return query_one(
        """SELECT * FROM documents WHERE content_hash=? AND university_id=? AND id<>?
           AND status='indexed' LIMIT 1""",
        (content_hash, university_id, exclude_id),
    )


def delete_document_row(doc_id: int) -> None:
    with tx() as c:
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))


# --- Runs & errors ------------------------------------------------------------------
def start_run(kind: str, university_id: str | None) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO ingestion_runs (university_id, kind, started_at) VALUES (?,?,?)",
            (university_id, kind, now_iso()),
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, summary: dict[str, Any]) -> None:
    with tx() as c:
        c.execute(
            "UPDATE ingestion_runs SET status=?, finished_at=?, summary=? WHERE id=?",
            (status, now_iso(), json.dumps(summary, ensure_ascii=False), run_id),
        )


def log_error(run_id: int | None, stage: str, message: str, url: str | None = None,
              document_id: int | None = None) -> None:
    with tx() as c:
        c.execute(
            """INSERT INTO ingestion_errors (run_id, document_id, url, stage, message, created_at)
               VALUES (?,?,?,?,?,?)""",
            (run_id, document_id, url, stage, str(message)[:2000], now_iso()),
        )


# --- Courses ------------------------------------------------------------------------
def replace_courses(document_id: int, university_id: str, courses: list[dict[str, Any]]) -> int:
    """Store structured course rows (and their requisite relations) for one document."""
    with tx() as c:
        c.execute("DELETE FROM courses WHERE document_id=?", (document_id,))
        n = 0
        for co in courses:
            cur = c.execute(
                """INSERT OR IGNORE INTO courses (university_id, document_id, code, title, credits,
                   credits_theory, credits_lab, department, program, category, semester,
                   requisites_raw, page, description, description_page)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (university_id, document_id, co["code"], co.get("title", ""),
                 co.get("credits", ""), co.get("credits_theory", ""), co.get("credits_lab", ""),
                 co.get("department", ""), co.get("program", ""), co.get("category", ""),
                 co.get("semester", ""), co.get("requisites_raw", ""), co.get("page", 0),
                 co.get("description", ""), co.get("description_page", 0)),
            )
            if cur.rowcount == 0:
                continue
            n += 1
            for req in co.get("requisites", []):
                c.execute(
                    """INSERT INTO course_requisites (course_id, kind, required_code, raw)
                       VALUES (?,?,?,?)""",
                    (cur.lastrowid, req["kind"], req.get("code", ""), req.get("raw", "")),
                )
        return n


def replace_programs(document_id: int, university_id: str, programs: list[dict[str, Any]]) -> int:
    with tx() as c:
        c.execute("DELETE FROM programs WHERE document_id=?", (document_id,))
        for p in programs:
            c.execute(
                """INSERT INTO programs (university_id, document_id, name, faculty, campus, degree,
                   min_average, streams, academic_year) VALUES (?,?,?,?,?,?,?,?,?)""",
                (university_id, document_id, p["name"], p.get("faculty", ""),
                 p.get("campus", ""), p.get("degree", ""), p.get("min_average", ""),
                 p.get("streams", ""), p.get("academic_year", "")),
            )
    return len(programs)


def boilerplate_hashes(university_id: str) -> set[str]:
    return {r["hash"] for r in query("SELECT hash FROM boilerplate WHERE university_id=?",
                                     (university_id,))}


def add_boilerplate(university_id: str, items: dict[str, str]) -> None:
    with tx() as c:
        c.executemany(
            "INSERT OR IGNORE INTO boilerplate (university_id, hash, sample) VALUES (?,?,?)",
            [(university_id, h, s[:300]) for h, s in items.items()],
        )


def course_with_relations(university_id: str, code: str) -> list[dict[str, Any]]:
    """All stored rows for a course code, with requisites, dependents and source document."""
    rows = query(
        """SELECT c.*, d.title AS document_title, d.url AS document_url, d.file_ref,
                  d.source_type, d.collection, d.version, d.retrieved_at
           FROM courses c JOIN documents d ON d.id=c.document_id
           WHERE c.university_id=? AND c.code=?""",
        (university_id, code),
    )
    for r in rows:
        r["requisites"] = query(
            "SELECT kind, required_code, raw FROM course_requisites WHERE course_id=?", (r["id"],)
        )
        for req in r["requisites"]:
            if req["required_code"]:
                known = query_one(
                    "SELECT 1 AS ok FROM courses WHERE university_id=? AND code=? LIMIT 1",
                    (university_id, req["required_code"]),
                )
                req["resolved"] = bool(known)
        r["required_by"] = query(
            """SELECT DISTINCT c.code, c.title, r.kind FROM course_requisites r
               JOIN courses c ON c.id=r.course_id
               WHERE r.required_code=? AND c.university_id=?""",
            (code, university_id),
        )
    return rows
