"""UniGuide AI — FastAPI backend.

Run:  uvicorn app.main:app --port 8000   (from the backend/ folder)
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db, vectorstore
from .config import ALL_COLLECTIONS, COLLECTION_HACKATHON, settings
from .ingest import pipeline
from .ingest.importers import SUPPORTED
from .ingest.web import host_allowed, is_private_or_unsafe
from .rag.answer import answer
from .rag.llm import get_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="UniGuide AI", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="رمز المسؤول غير صحيح")


def _background(target, *args, **kwargs) -> None:
    def run() -> None:
        try:
            target(*args, **kwargs)
        except pipeline.IngestionBusy:
            pass
        except Exception:  # noqa: BLE001 - already logged and recorded in ingestion_runs
            logging.getLogger("uniguide").exception("background ingestion failed")

    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    llm = get_llm()
    return {
        "ok": True,
        "llm": llm.label if llm else None,
        "mode": llm.name if llm else "extractive",
        "embedding_model": settings.embedding_model,
        "collections": {c: vectorstore.count(c) for c in ALL_COLLECTIONS},
        "ingestion_running": pipeline.is_running(),
    }


@app.get("/api/universities")
def universities() -> list[dict[str, Any]]:
    out = []
    for u in db.list_universities():
        n = db.query_one("SELECT COUNT(*) AS n FROM documents WHERE university_id=? AND "
                         "status='indexed'", (u["id"],))["n"]
        out.append({"id": u["id"], "name_ar": u["name_ar"], "name_en": u["name_en"],
                    "allowed_domains": u["allowed_domains"], "indexed_documents": n})
    return out


class UniversityIn(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    name_ar: str
    name_en: str = ""
    aliases: list[str] = []
    allowed_domains: list[str] = []


@app.post("/api/universities", dependencies=[Depends(require_admin)])
def add_university(body: UniversityIn) -> dict[str, Any]:
    if db.get_university(body.id):
        raise HTTPException(409, "الجامعة موجودة مسبقاً")
    aliases = list(dict.fromkeys([body.name_ar, body.name_en, *body.aliases]))
    return db.add_university(body.id, body.name_ar, body.name_en, [a for a in aliases if a],
                             [d.lower() for d in body.allowed_domains])


# --------------------------------------------------------------------------------------
class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    university_id: str | None = None


@app.post("/api/chat")
def chat(body: ChatIn) -> dict[str, Any]:
    return answer(body.question, body.university_id)


# --------------------------------------------------------------------------------------
def _doc_out(d: dict[str, Any]) -> dict[str, Any]:
    d = dict(d)
    d.pop("local_path", None)
    d["extra"] = json.loads(d.get("extra") or "{}")
    d["has_file"] = bool(db.get_document(d["id"]).get("local_path"))
    return d


@app.get("/api/sources")
def sources(university_id: str | None = None, collection: str | None = None,
            status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM documents WHERE 1=1"
    params: list[Any] = []
    for col, val in (("university_id", university_id), ("collection", collection),
                     ("status", status)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    sql += " ORDER BY updated_at DESC"
    return [_doc_out(d) for d in db.query(sql, tuple(params))]


@app.get("/api/sources/stats")
def source_stats() -> dict[str, Any]:
    rows = db.query("""SELECT university_id, collection, source_type, status, COUNT(*) AS n,
                              SUM(chunk_count) AS chunks, MAX(retrieved_at) AS last
                       FROM documents GROUP BY university_id, collection, source_type, status""")
    programs = db.query("SELECT university_id, COUNT(*) AS n FROM programs GROUP BY university_id")
    courses = db.query("SELECT university_id, COUNT(DISTINCT code) AS n FROM courses "
                       "GROUP BY university_id")
    last_run = db.query_one("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT 1")
    if last_run:
        last_run["summary"] = json.loads(last_run["summary"])
    return {
        "groups": rows,
        "programs": programs,
        "courses": courses,
        "collections": {c: vectorstore.count(c) for c in ALL_COLLECTIONS},
        "hackathon_dataset_present": vectorstore.count(COLLECTION_HACKATHON) > 0,
        "ingestion_running": pipeline.is_running(),
        "last_run": last_run,
    }


class UrlIn(BaseModel):
    university_id: str
    url: str


@app.post("/api/sources/url", dependencies=[Depends(require_admin)])
def add_url(body: UrlIn) -> dict[str, Any]:
    uni = db.get_university(body.university_id)
    if not uni:
        raise HTTPException(404, "جامعة غير معروفة")
    url = body.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "الرابط يجب أن يبدأ بـ http أو https")
    if not host_allowed(url, uni["allowed_domains"]):
        raise HTTPException(400, "الرابط ليس ضمن النطاقات الرسمية المعتمدة لهذه الجامعة: "
                                 + ", ".join(uni["allowed_domains"]))
    if is_private_or_unsafe(url):
        raise HTTPException(400, "لا يُسمح بفهرسة بوابات الطلبة أو صفحات تسجيل الدخول")
    if pipeline.is_running():
        raise HTTPException(409, "عملية فهرسة أخرى قيد التنفيذ، حاول بعد انتهائها")
    _background(pipeline.run_website_ingestion, uni["id"], discover=False, urls=[url],
                max_pdfs=10, origin="admin")
    return {"started": True, "url": url}


class WebsiteRunIn(BaseModel):
    university_id: str = "alquds"
    discover: bool = True
    max_pages: int | None = Field(default=None, ge=1, le=200)
    max_pdfs: int | None = Field(default=None, ge=0, le=100)


@app.post("/api/ingest/website", dependencies=[Depends(require_admin)])
def start_website(body: WebsiteRunIn) -> dict[str, Any]:
    if not db.get_university(body.university_id):
        raise HTTPException(404, "جامعة غير معروفة")
    if pipeline.is_running():
        raise HTTPException(409, "عملية فهرسة أخرى قيد التنفيذ")
    _background(pipeline.run_website_ingestion, body.university_id, discover=body.discover,
                max_pages=body.max_pages, max_pdfs=body.max_pdfs)
    return {"started": True}


class RefreshIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


@app.post("/api/sources/refresh", dependencies=[Depends(require_admin)])
def refresh(body: RefreshIn) -> dict[str, Any]:
    if pipeline.is_running():
        raise HTTPException(409, "عملية فهرسة أخرى قيد التنفيذ")
    _background(pipeline.refresh_documents, body.ids)
    return {"started": True, "count": len(body.ids)}


@app.delete("/api/sources/{doc_id}", dependencies=[Depends(require_admin)])
def delete_source(doc_id: int) -> dict[str, Any]:
    if not pipeline.remove_document(doc_id):
        raise HTTPException(404, "المستند غير موجود")
    return {"deleted": doc_id}


@app.post("/api/sources/upload", dependencies=[Depends(require_admin)])
def upload(file: UploadFile = File(...), university_id: str = Form(...),
           target: str = Form("uploads"), title: str = Form(""), version: str = Form(""),
           academic_year: str = Form(""), program: str = Form(""),
           department: str = Form("")) -> dict[str, Any]:
    uni = db.get_university(university_id)
    if not uni:
        raise HTTPException(404, "جامعة غير معروفة")
    name = Path(file.filename or "upload").name
    if Path(name).suffix.lower() not in SUPPORTED:
        raise HTTPException(400, "نوع الملف غير مدعوم (PDF, TXT, CSV, JSON, XLSX)")
    collection = COLLECTION_HACKATHON if target == "hackathon" else uni["uploads_collection"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(name).suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        return pipeline.import_upload(tmp_path, name, uni["id"], collection, title=title,
                                      version=version, academic_year=academic_year,
                                      program=program, department=department)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: int) -> FileResponse:
    doc = db.get_document(doc_id)
    if not doc or not doc.get("local_path") or not Path(doc["local_path"]).exists():
        raise HTTPException(404, "الملف الأصلي غير متوفر")
    path = Path(doc["local_path"])
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, filename=doc.get("file_ref") or path.name,
                        content_disposition_type="inline")


# --------------------------------------------------------------------------------------
@app.get("/api/ingest/runs")
def runs(limit: int = Query(20, le=100)) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["summary"] = json.loads(r["summary"])
        r["summary"].pop("results", None)
    return rows


@app.get("/api/errors")
def errors(limit: int = Query(100, le=500)) -> list[dict[str, Any]]:
    """Current, unresolved problems only: the latest error per URL whose source still fails.

    The full history stays in the database (and per run in /api/ingest/runs)."""
    return db.query(
        """SELECT e.* FROM ingestion_errors e
           LEFT JOIN documents d ON d.id = e.document_id
           WHERE e.id IN (SELECT MAX(id) FROM ingestion_errors GROUP BY COALESCE(url, message))
             AND (e.document_id IS NULL OR d.status = 'failed')
           ORDER BY e.id DESC LIMIT ?""", (limit,))


@app.get("/api/programs")
def programs(university_id: str = "alquds") -> list[dict[str, Any]]:
    return db.query("""SELECT p.*, d.url AS source_url, d.title AS source_title, d.retrieved_at
                       FROM programs p JOIN documents d ON d.id=p.document_id
                       WHERE p.university_id=? ORDER BY p.faculty, p.name""", (university_id,))


@app.get("/api/courses")
def courses(university_id: str = "alquds", q: str = "") -> list[dict[str, Any]]:
    like = f"%{q}%"
    return db.query("""SELECT c.code, c.title, c.credits, c.category, c.semester, c.page,
                              c.requisites_raw, c.program, d.title AS document_title, d.id AS doc_id
                       FROM courses c JOIN documents d ON d.id=c.document_id
                       WHERE c.university_id=? AND (c.code LIKE ? OR c.title LIKE ?)
                       ORDER BY c.code LIMIT 300""", (university_id, like, like))


@app.get("/api/courses/{code}")
def course(code: str, university_id: str = "alquds") -> list[dict[str, Any]]:
    rows = db.course_with_relations(university_id, code)
    if not rows:
        raise HTTPException(404, "المساق غير موجود في الوثائق المفهرسة")
    return rows
