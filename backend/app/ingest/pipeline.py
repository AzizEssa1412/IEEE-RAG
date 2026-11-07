"""Ingestion orchestration.

Website URL -> fetch HTML -> extract main content -> clean Arabic -> metadata -> chunk ->
embed -> ChromaDB, with every outcome (including failures) recorded in SQLite.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .. import db, vectorstore
from ..config import settings
from ..text.chunking import Block, chunk_blocks, content_hash
from .importers import ImportError_, import_file
from .pdf import extract_pdf
from .web import PageContent, PoliteFetcher, canonical_url, extract_page, relevance_score

log = logging.getLogger("uniguide.ingest")

# Official starting points supplied in the specification (all public, no login).
ALQUDS_SEEDS = [
    "https://www.alquds.edu/ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/registration-ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/admissions-ar/admission-guides-ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/ba2026-ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/admissions-ar/bachelors-program-ar/offered-programs-ar/",
    # Additional official academic pages found in the site's page sitemap:
    "https://www.alquds.edu/ar/academic-calendar-ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/registration-ar/document-issuance-procedure-ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/registration-ar/registration-forms-ar/",
    "https://www.alquds.edu/ar/admissions-registration-ar/registration-ar/registration-announcements-ar/",
]
SITEMAPS = {"alquds": ["https://www.alquds.edu/page-sitemap.xml"]}
SEEDS = {"alquds": ALQUDS_SEEDS}

_RUN_LOCK = threading.Lock()


class IngestionBusy(Exception):
    pass


def is_running() -> bool:
    return _RUN_LOCK.locked()


# --------------------------------------------------------------------------------------
# Indexing (shared by every source type)
# --------------------------------------------------------------------------------------
def index_blocks(doc_id: int, university: dict, collection: str, source_type: str,
                 title: str, blocks: list[Block], *, url: str = "", file_ref: str = "",
                 version: str = "", academic_year: str = "", retrieved_at: str = "",
                 parent_url: str = "") -> tuple[int, int]:
    """Chunk + embed + store. Returns (chunks stored, duplicate chunks skipped)."""
    chunks = chunk_blocks(blocks)
    vectorstore.delete_document(collection, doc_id)
    already = vectorstore.existing_chunk_hashes(collection, university["id"],
                                                [c.hash for c in chunks])
    seen: set[str] = set()
    texts, embed_texts, metas = [], [], []
    skipped = 0
    for c in chunks:
        if c.hash in already or c.hash in seen or not _meaningful(c.text):
            skipped += 1
            continue
        seen.add(c.hash)
        texts.append(c.text)
        embed_texts.append(f"{title} | {c.section}\n{c.text}" if c.section else f"{title}\n{c.text}")
        metas.append({
            "university_id": university["id"],
            "university_name": university["name_ar"],
            "collection": collection,
            "source_type": source_type,
            "doc_id": doc_id,
            "title": title,
            "url": url,
            "file_ref": file_ref,
            "parent_url": parent_url,
            "page": c.page,
            "section": c.section,
            "article": c.article,
            "academic_year": academic_year,
            "version": version,
            "retrieved_at": retrieved_at,
            "chunk_index": len(metas),
            "chunk_hash": c.hash,
        })
    stored = vectorstore.add_chunks(collection, doc_id, texts, embed_texts, metas)
    return stored, skipped


_NOISE_TOKENS = re.compile(r"(https?://\S+|www\.\S+|\b\w+\.(?:edu|com|org)\b|(?:\b\w\s){3,}\w\b)",
                           re.IGNORECASE)


_GENERIC_ANCHORS = re.compile(
    r"^(إ?اضغط هنا|إضغط هنا|هنا|اضغط|تحميل|للتحميل|المزيد|اقرأ المزيد|رابط|click here|here|"
    r"download|more|read more|link)\b", re.IGNORECASE)


def is_descriptive_anchor(anchor: str) -> bool:
    """«إضغط هنا لتحميل…» / "click here" say nothing about the document."""
    a = (anchor or "").strip()
    return len(a) > 6 and not _GENERIC_ANCHORS.match(a)


def _meaningful(text: str) -> bool:
    """Skip chunks that are only a heading plus URLs / letter-spaced web addresses."""
    core = _NOISE_TOKENS.sub(" ", text)
    return len(re.sub(r"\s+", " ", core).strip()) >= 40


def _programs_from_page(pc: PageContent) -> list[dict[str, str]]:
    fields = {"التخصص": "name", "الكلية": "faculty", "مكان الدراسة": "campus",
              "الحد الأدنى للمعدل": "min_average", "فرع الثانوية العامة": "streams"}
    out = []
    for t in pc.data_tables:
        if "التخصص" not in t["headers"]:
            continue
        idx = {fields[h]: i for i, h in enumerate(t["headers"]) if h in fields}
        for row in t["rows"]:
            p = {k: row[i] for k, i in idx.items() if i < len(row)}
            if p.get("name"):
                p["degree"] = "بكالوريوس" if "بكالوريوس" in pc.title + pc.text[:400] else ""
                p["academic_year"] = pc.academic_year
                out.append(p)
    return out


# --------------------------------------------------------------------------------------
# Website pages & linked PDFs
# --------------------------------------------------------------------------------------
def _record_failure(doc_id: int, run_id: int | None, stage: str, message: str, url: str) -> None:
    db.update_document(doc_id, status="failed", error=message, retrieved_at=db.now_iso())
    db.log_error(run_id, stage, message, url=url, document_id=doc_id)


def _record_skip(doc_id: int, reason: str) -> None:
    """Expected, by-design outcomes (policy, link-only pages…): visible on the source, not errors."""
    db.update_document(doc_id, status="skipped", error=reason, retrieved_at=db.now_iso())


MSG_LINK_HUB = ("صفحة فهرس روابط بدون نص أكاديمي خاص بها؛ الصفحات والوثائق المرتبطة بها "
                "تُفهرس كمصادر مستقلة")
MSG_CALENDAR_FEED = ("محتوى التقويم يُعرض داخل إطار (iframe)؛ تم استيراده من خلاصة التقويم "
                     "الأكاديمي الرسمية بدلاً من الصفحة")
MSG_BROKEN_LINK = "رابط معطّل على موقع الجامعة نفسه ({error}) — لا يوجد محتوى لفهرسته"
MSG_SCANNED = ("ملف PDF ممسوح ضوئياً بدون طبقة نصية — لم تتم فهرسته (OCR غير مفعّل)؛ "
               "الملف محفوظ ويمكن فتحه")


def _fetch_problem(doc_id: int, run_id: int | None, res: Any, url: str, origin: str) -> str:
    """Policy refusals and broken links found while crawling are skips; anything else is an error."""
    if res.stage == "policy":
        _record_skip(doc_id, res.error)
        return "skipped"
    if origin != "seed" and str(res.error).startswith("HTTP 404"):
        _record_skip(doc_id, MSG_BROKEN_LINK.format(error=res.error))
        return "skipped"
    _record_failure(doc_id, run_id, res.stage, res.error, url)
    return "failed"


def _index_page(doc_id: int, university: dict, pc: PageContent, url: str, run_id: int | None,
                boiler: set[str], retrieved_at: str) -> dict[str, Any]:
    collection = university["web_collection"]
    blocks = [b for b in pc.blocks if content_hash(b.text) not in boiler]
    text = "\n".join(b.text for b in blocks)
    if len(text) < 80:
        if university["id"] == "alquds" and "/academic-calendar-ar" in url:
            _record_skip(doc_id, MSG_CALENDAR_FEED)
            return {"url": url, "status": "skipped", "reason": MSG_CALENDAR_FEED}
        if pc.pdf_links or pc.links:
            _record_skip(doc_id, MSG_LINK_HUB)
            return {"url": url, "status": "skipped", "reason": MSG_LINK_HUB}
        msg = ("لم يُعثر على محتوى أكاديمي رئيسي في الصفحة (الصفحة فارغة أو تُعرض بالجافاسكربت "
               "أو تحتوي على قوائم فقط)")
        _record_failure(doc_id, run_id, "extract", msg, url)
        return {"url": url, "status": "failed", "error": msg}
    h = content_hash(text)
    prev = db.get_document(doc_id) or {}
    version = pc.date_modified or pc.date_published
    if prev.get("content_hash") == h and prev.get("status") == "indexed":
        db.update_document(doc_id, retrieved_at=retrieved_at, version=version, error=None)
        vectorstore.update_document_metadata(collection, doc_id, {"retrieved_at": retrieved_at})
        return {"url": url, "status": "unchanged", "chunks": prev.get("chunk_count", 0)}
    dup = db.find_duplicate(h, university["id"], doc_id)
    if dup:
        msg = f"محتوى مكرر لمستند مفهرس مسبقاً (#{dup['id']}: {dup['title']})"
        db.update_document(doc_id, status="duplicate", error=msg, content_hash=h,
                           retrieved_at=retrieved_at, title=pc.title)
        return {"url": url, "status": "duplicate", "duplicate_of": dup["id"]}
    stored, skipped = index_blocks(
        doc_id, university, collection, "website", pc.title or unquote(url), blocks, url=url,
        version=version, academic_year=pc.academic_year, retrieved_at=retrieved_at)
    programs = _programs_from_page(pc)
    if programs:
        db.replace_programs(doc_id, university["id"], programs)
    db.update_document(doc_id, status="indexed", error=None, content_hash=h, title=pc.title,
                       version=version, academic_year=pc.academic_year, chunk_count=stored,
                       retrieved_at=retrieved_at)
    return {"url": url, "status": "indexed", "chunks": stored, "duplicate_chunks": skipped,
            "programs": len(programs), "method": pc.method}


def _ingest_pdf_url(url: str, anchor: str, parent: str, university: dict, run_id: int | None,
                    fetcher: PoliteFetcher, origin: str) -> dict[str, Any]:
    collection = university["web_collection"]
    doc_id = db.upsert_document(university_id=university["id"], collection=collection,
                                source_type="pdf", url=url, title=anchor or Path(unquote(url)).name,
                                origin=origin, parent_url=parent)
    retrieved_at = db.now_iso()
    res = fetcher.fetch(url, university["allowed_domains"], max_bytes=settings.max_pdf_bytes)
    if not res.ok:
        return {"url": url, "status": _fetch_problem(doc_id, run_id, res, url, origin),
                "error": res.error}
    if not res.content.startswith(b"%PDF"):
        msg = f"الرابط لا يعيد ملف PDF صالحاً (content-type: {res.content_type})"
        _record_failure(doc_id, run_id, "pdf", msg, url)
        return {"url": url, "status": "failed", "error": msg}
    local = settings.files_dir / f"doc{doc_id}.pdf"
    local.write_bytes(res.content)
    try:
        pc = extract_pdf(res.content)
    except Exception as e:  # noqa: BLE001
        _record_failure(doc_id, run_id, "pdf", f"تعذّر تحليل ملف PDF: {e}", url)
        return {"url": url, "status": "failed", "error": str(e)}
    if pc.text_pages == 0 or len(pc.text.strip()) < 50:
        _record_skip(doc_id, MSG_SCANNED)
        db.update_document(doc_id, local_path=str(local), page_count=pc.page_count)
        return {"url": url, "status": "skipped", "reason": MSG_SCANNED}
    # Link text is the most reliable human title; embedded PDF metadata titles are often junk.
    title = anchor if is_descriptive_anchor(anchor) else Path(unquote(url)).stem
    h = content_hash(pc.text)
    prev = db.get_document(doc_id) or {}
    version = pc.version or res.headers.get("last-modified", "")
    if prev.get("content_hash") == h and prev.get("status") == "indexed":
        db.update_document(doc_id, retrieved_at=retrieved_at, error=None, local_path=str(local))
        vectorstore.update_document_metadata(collection, doc_id, {"retrieved_at": retrieved_at})
        return {"url": url, "status": "unchanged"}
    dup = db.find_duplicate(h, university["id"], doc_id)
    if dup:
        db.update_document(doc_id, status="duplicate", content_hash=h, retrieved_at=retrieved_at,
                           error=f"نسخة مكررة من المستند #{dup['id']}", local_path=str(local))
        return {"url": url, "status": "duplicate", "duplicate_of": dup["id"]}
    # A factual "document card" so questions like «وين بلاقي دليل القبول؟» find the file itself.
    parent_doc = db.query_one("SELECT title FROM documents WHERE url=? AND university_id=?",
                              (parent, university["id"])) if parent else None
    card = (f"وثيقة رسمية: {title}\nنوع الملف: PDF ({pc.page_count} صفحة)\n"
            f"منشورة على الموقع الرسمي لـ{university['name_ar']} على الرابط: {url}")
    if parent_doc:
        card += f"\nيمكن الوصول إليها من صفحة: {parent_doc['title']} ({parent})"
    blocks = [Block(card, "para", 1, "بطاقة الوثيقة")] + pc.blocks
    stored, skipped = index_blocks(doc_id, university, collection, "pdf", title, blocks,
                                   url=url, version=version, academic_year=pc.academic_year,
                                   retrieved_at=retrieved_at, parent_url=parent)
    if pc.courses:
        db.replace_courses(doc_id, university["id"], pc.courses)
    db.update_document(doc_id, status="indexed", error=None, content_hash=h, title=title,
                       version=version, academic_year=pc.academic_year, chunk_count=stored,
                       page_count=pc.page_count, retrieved_at=retrieved_at, local_path=str(local))
    return {"url": url, "status": "indexed", "chunks": stored, "courses": len(pc.courses)}


def _sitemap_urls(fetcher: PoliteFetcher, university: dict, run_id: int) -> list[str]:
    urls: list[str] = []
    for sm in SITEMAPS.get(university["id"], []):
        res = fetcher.fetch(sm, university["allowed_domains"])
        if not res.ok:
            db.log_error(run_id, res.stage, f"تعذّر قراءة خريطة الموقع: {res.error}", url=sm)
            continue
        urls += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", res.text)
    return [canonical_url(u) for u in urls]


def run_website_ingestion(university_id: str, discover: bool = True,
                          urls: list[str] | None = None, max_pages: int | None = None,
                          max_pdfs: int | None = None, include_pdfs: bool = True,
                          origin: str = "seed") -> dict[str, Any]:
    if not _RUN_LOCK.acquire(blocking=False):
        raise IngestionBusy("عملية فهرسة أخرى قيد التنفيذ")
    university = db.get_university(university_id)
    if university is None:
        _RUN_LOCK.release()
        raise ValueError(f"جامعة غير معروفة: {university_id}")
    run_id = db.start_run("website" if urls is None else "url", university_id)
    fetcher = PoliteFetcher()
    max_pages = max_pages or settings.max_pages_per_run
    max_pdfs = settings.max_pdfs_per_run if max_pdfs is None else max_pdfs
    results: list[dict[str, Any]] = []
    try:
        start = [canonical_url(u) for u in (urls or SEEDS.get(university_id, []))]
        queue: list[tuple[str, str, str]] = [(u, origin, "") for u in start]  # url, origin, parent
        fetched: dict[str, tuple[int, PageContent, str]] = {}
        pdf_candidates: dict[str, tuple[str, str, int]] = {}
        discovered: dict[str, int] = {}

        # ---- phase 1: fetch + extract (no indexing yet) -------------------------------
        i = 0
        while i < len(queue) and len(fetched) < max_pages:
            url, org, parent = queue[i]
            i += 1
            if url in fetched:
                continue
            doc_id = db.upsert_document(university_id=university_id,
                                        collection=university["web_collection"],
                                        source_type="website", url=url, origin=org,
                                        parent_url=parent or None, title=unquote(url))
            res = fetcher.fetch(url, university["allowed_domains"])
            if not res.ok:
                results.append({"url": url, "error": res.error,
                                "status": _fetch_problem(doc_id, run_id, res, url, org)})
                continue
            if not res.content_type.startswith("text/html"):
                msg = f"نوع محتوى غير متوقع: {res.content_type}"
                _record_failure(doc_id, run_id, "fetch", msg, url)
                results.append({"url": url, "status": "failed", "error": msg})
                continue
            try:
                pc = extract_page(res.text, url)
            except Exception as e:  # noqa: BLE001
                _record_failure(doc_id, run_id, "extract", f"فشل استخراج المحتوى: {e}", url)
                results.append({"url": url, "status": "failed", "error": str(e)})
                continue
            fetched[url] = (doc_id, pc, db.now_iso())
            for pdf, anchor in pc.pdf_links:
                if pdf not in pdf_candidates:
                    pdf_candidates[pdf] = (anchor, url, relevance_score(pdf, anchor))
            if discover:
                for link, anchor in pc.links:
                    s = relevance_score(link, anchor)
                    if s > 0 and "/ar/" in link:
                        discovered[link] = max(discovered.get(link, 0), s + 1)
            if discover and i == len(start):
                for u in _sitemap_urls(fetcher, university, run_id):
                    s = relevance_score(u)
                    if s > 0 and "/ar/" in u:
                        discovered[u] = max(discovered.get(u, 0), s)
            if discover and i >= len(start):
                ranked = sorted((u for u in discovered if u not in fetched
                                 and u not in {q[0] for q in queue}),
                                key=lambda u: -discovered[u])
                queue += [(u, "discovered", "") for u in ranked[:max_pages]]

        # ---- phase 2: template/boilerplate detection across pages ---------------------
        boiler = db.boilerplate_hashes(university_id)
        if len(fetched) >= 3:
            freq: Counter[str] = Counter()
            sample: dict[str, str] = {}
            for _, pc, _ in fetched.values():
                hs = {content_hash(b.text) for b in pc.blocks}
                for b in pc.blocks:
                    sample.setdefault(content_hash(b.text), b.text)
                freq.update(hs)
            threshold = max(3, int(len(fetched) * 0.3))
            new = {h: sample[h] for h, n in freq.items() if n >= threshold}
            if new:
                db.add_boilerplate(university_id, new)
                boiler |= set(new)

        # ---- phase 3: index pages --------------------------------------------------
        for url, (doc_id, pc, ts) in fetched.items():
            try:
                results.append(_index_page(doc_id, university, pc, url, run_id, boiler, ts))
            except Exception as e:  # noqa: BLE001
                _record_failure(doc_id, run_id, "index", f"فشل الفهرسة: {e}", url)
                results.append({"url": url, "status": "failed", "error": str(e)})

        # ---- phase 4: linked PDFs from allowed pages -------------------------------
        if include_pdfs and max_pdfs:
            ranked_pdfs = sorted(pdf_candidates.items(), key=lambda kv: -kv[1][2])[:max_pdfs]
            for pdf, (anchor, parent, _) in ranked_pdfs:
                try:
                    results.append(_ingest_pdf_url(pdf, anchor, parent, university, run_id,
                                                   fetcher, "discovered"))
                except Exception as e:  # noqa: BLE001
                    db.log_error(run_id, "pdf", str(e), url=pdf)
                    results.append({"url": pdf, "status": "failed", "error": str(e)})

        # ---- phase 5: official academic calendar (public feed behind the calendar page) ----
        if university_id == "alquds" and urls is None:
            from .calendar import ingest_alquds_calendar

            try:
                results += ingest_alquds_calendar(university, run_id, fetcher)
            except Exception as e:  # noqa: BLE001
                db.log_error(run_id, "calendar", f"فشل استيراد التقويم الأكاديمي: {e}")

        summary = _summarize(results)
        db.finish_run(run_id, "finished", summary)
        return {"run_id": run_id, **summary, "results": results}
    except Exception as e:  # noqa: BLE001
        log.exception("ingestion run failed")
        db.log_error(run_id, "run", f"توقفت عملية الفهرسة: {e}")
        db.finish_run(run_id, "failed", {**_summarize(results), "error": str(e)})
        raise
    finally:
        fetcher.close()
        _RUN_LOCK.release()


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(r["status"] for r in results)
    return {"total": len(results), "indexed": c.get("indexed", 0),
            "unchanged": c.get("unchanged", 0), "failed": c.get("failed", 0),
            "duplicate": c.get("duplicate", 0), "skipped": c.get("skipped", 0),
            "chunks": sum(r.get("chunks", 0) for r in results if r["status"] == "indexed")}


def refresh_documents(doc_ids: list[int]) -> dict[str, Any]:
    """Re-fetch selected website pages / linked PDFs (uploads are re-parsed from disk)."""
    if not _RUN_LOCK.acquire(blocking=False):
        raise IngestionBusy("عملية فهرسة أخرى قيد التنفيذ")
    results = []
    run_id = db.start_run("refresh", None)
    fetcher = PoliteFetcher()
    try:
        for doc_id in doc_ids:
            doc = db.get_document(doc_id)
            if not doc:
                results.append({"id": doc_id, "status": "failed", "error": "غير موجود"})
                continue
            uni = db.get_university(doc["university_id"])
            if doc["origin"] == "upload":
                results.append(_reindex_upload(doc, uni, run_id))
            elif "calendar_feed" in (doc.get("extra") or ""):
                from .calendar import refresh_calendar_doc

                results.append(refresh_calendar_doc(doc, uni, run_id, fetcher))
            elif doc["source_type"] == "pdf":
                results.append(_ingest_pdf_url(doc["url"], doc["title"], doc["parent_url"] or "",
                                               uni, run_id, fetcher, doc["origin"]))
            else:
                res = fetcher.fetch(doc["url"], uni["allowed_domains"])
                if not res.ok:
                    results.append({"url": doc["url"], "error": res.error, "status": _fetch_problem(
                        doc_id, run_id, res, doc["url"], doc["origin"])})
                    continue
                pc = extract_page(res.text, doc["url"])
                results.append(_index_page(doc_id, uni, pc, doc["url"], run_id,
                                           db.boilerplate_hashes(uni["id"]), db.now_iso()))
        summary = _summarize(results)
        db.finish_run(run_id, "finished", summary)
        return {"run_id": run_id, **summary, "results": results}
    finally:
        fetcher.close()
        _RUN_LOCK.release()


# --------------------------------------------------------------------------------------
# Uploaded files
# --------------------------------------------------------------------------------------
def import_upload(src: Path, original_name: str, university_id: str, collection: str,
                  title: str = "", version: str = "", academic_year: str = "",
                  program: str = "", department: str = "") -> dict[str, Any]:
    university = db.get_university(university_id)
    if university is None:
        raise ValueError(f"جامعة غير معروفة: {university_id}")
    run_id = db.start_run("upload", university_id)
    doc_id = db.upsert_document(university_id=university_id, collection=collection,
                                source_type="pending", file_ref=original_name,
                                title=title or Path(original_name).stem, origin="upload")
    stored_path = settings.files_dir / f"doc{doc_id}{Path(original_name).suffix.lower()}"
    shutil.copyfile(src, stored_path)
    db.update_document(doc_id, local_path=str(stored_path))
    meta = {"title": title, "version": version, "academic_year": academic_year,
            "program": program, "department": department}
    db.update_document(doc_id, extra=json.dumps(meta, ensure_ascii=False))
    result = _reindex_upload(db.get_document(doc_id), university, run_id)
    db.finish_run(run_id, "finished" if result["status"] != "failed" else "failed", result)
    return {"id": doc_id, **result}


def _reindex_upload(doc: dict, university: dict, run_id: int | None) -> dict[str, Any]:
    doc_id = doc["id"]
    meta = json.loads(doc.get("extra") or "{}")  # admin-supplied metadata from the upload form
    retrieved_at = db.now_iso()
    try:
        content = import_file(Path(doc["local_path"]), doc["file_ref"])
    except ImportError_ as e:
        db.update_document(doc_id, status="failed", error=str(e), retrieved_at=retrieved_at)
        db.log_error(run_id, "import", str(e), url=doc["file_ref"], document_id=doc_id)
        return {"file": doc["file_ref"], "status": "failed", "error": str(e)}
    title = meta.get("title") or content.title
    version = meta.get("version") or content.version
    year = meta.get("academic_year") or content.academic_year
    for co in content.courses:
        co["program"] = co.get("program") or meta.get("program", "")
        co["department"] = co.get("department") or meta.get("department", "")
    h = content_hash(content.text)
    dup = db.find_duplicate(h, university["id"], doc_id)
    if dup:
        msg = f"الملف مطابق لمستند مفهرس مسبقاً (#{dup['id']}: {dup['title']})"
        db.update_document(doc_id, status="duplicate", error=msg, content_hash=h,
                           source_type=content.source_type, retrieved_at=retrieved_at)
        return {"file": doc["file_ref"], "status": "duplicate", "duplicate_of": dup["id"]}
    # Facts about the document itself (from its own metadata) so "when was the plan issued?"
    # or "which program is this?" can be answered and checked like any other passage.
    card = [f"وثيقة رسمية: {title}", f"الجامعة: {university['name_ar']}"]
    if version:
        card.append(f"الإصدار / Version (تاريخ صدور الوثيقة): {version}")
    programs = sorted({co.get("program") for co in content.courses if co.get("program")})
    if programs:
        card.append("البرنامج / Program: " + "، ".join(programs))
    if content.page_count:
        card.append(f"عدد الصفحات: {content.page_count}")
    blocks = [Block("\n".join(card), "para", 1, "بطاقة الوثيقة")] + content.blocks
    stored, skipped = index_blocks(doc_id, university, doc["collection"], content.source_type,
                                   title, blocks, file_ref=doc["file_ref"],
                                   version=version, academic_year=year, retrieved_at=retrieved_at)
    n_courses = db.replace_courses(doc_id, university["id"], content.courses)
    db.update_document(doc_id, status="indexed", source_type=content.source_type, title=title,
                       version=version, academic_year=year, content_hash=h, chunk_count=stored,
                       page_count=content.page_count, retrieved_at=retrieved_at,
                       error="; ".join(content.warnings) or None)
    return {"file": doc["file_ref"], "status": "indexed", "chunks": stored,
            "duplicate_chunks": skipped, "courses": n_courses, "warnings": content.warnings}


def remove_document(doc_id: int) -> bool:
    doc = db.get_document(doc_id)
    if not doc:
        return False
    vectorstore.delete_document(doc["collection"], doc_id)
    if doc.get("local_path"):
        Path(doc["local_path"]).unlink(missing_ok=True)
    db.delete_document_row(doc_id)
    return True
