"""Al-Quds University academic calendar connector.

The official page https://www.alquds.edu/ar/academic-calendar-ar/ embeds a public calendar
application (iframe) that loads its rows from public XML feeds on apps.alquds.edu. Only that
exact calendar path is allowed here — not the rest of the apps subdomain — and robots.txt and
the crawl delay are honoured through the same PoliteFetcher used for the website.
"""
from __future__ import annotations

import re
from typing import Any

from .. import db
from ..text.arabic import clean_text
from ..text.chunking import Block, content_hash
from .web import PoliteFetcher

CALENDAR_PAGE = "https://www.alquds.edu/ar/academic-calendar-ar/"
FEED_BASE = "https://apps.alquds.edu/ar/acadaffair/acad_calendar"
FEED_DOMAINS = ["apps.alquds.edu"]
SEMESTERS = {"1": "الفصل الدراسي الأول", "2": "الفصل الدراسي الثاني", "3": "الفصل الصيفي"}
CAL_TYPE = ("U", "الجامعة")

_OPTION = re.compile(r"<option value='([^']+)'>")
_ROW = re.compile(r"<row[^>]*>(.*?)</row>", re.S)
_CELL = re.compile(r"<cell>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</cell>", re.S)


def parse_rows(xml: str) -> list[list[str]]:
    rows = []
    for row in _ROW.findall(xml):
        cells = [clean_text(re.sub(r"\s+", " ", c)) for c in _CELL.findall(row)]
        if cells and cells[0]:
            rows.append(cells)
    return rows


def calendar_blocks(rows: list[list[str]], title: str) -> list[Block]:
    blocks = [Block(title, "heading", 0, title)]
    for cells in rows:
        item, start, end = (cells + ["", "", ""])[:3]
        parts = [f"البند: {item}"]
        if start:
            parts.append(f"من: {start}")
        if end:
            parts.append(f"إلى: {end}")
        blocks.append(Block(" | ".join(parts), "table", 0, title))
    return blocks


def ingest_alquds_calendar(university: dict, run_id: int | None, fetcher: PoliteFetcher,
                           years: int = 2) -> list[dict[str, Any]]:
    from .pipeline import _record_failure, index_blocks  # local import avoids a cycle

    results: list[dict[str, Any]] = []
    terms = fetcher.fetch(f"{FEED_BASE}/Term_list", FEED_DOMAINS)
    if not terms.ok:
        db.log_error(run_id, terms.stage, f"تعذّر قراءة قائمة أعوام التقويم: {terms.error}",
                     url=f"{FEED_BASE}/Term_list")
        return [{"url": f"{FEED_BASE}/Term_list", "status": "failed", "error": terms.error}]
    year_list = [y for y in _OPTION.findall(terms.text) if y.isdigit()][:years]
    for year in year_list:
        for sem, sem_name in SEMESTERS.items():
            feed = f"{FEED_BASE}/calendar_dtl/{year}/{sem}/{CAL_TYPE[0]}"
            display_url = f"{CALENDAR_PAGE}#{year}-{sem}-{CAL_TYPE[0]}"
            academic_year = f"{year}/{int(year) + 1}"
            title = f"التقويم الأكاديمي {academic_year} – {sem_name} ({CAL_TYPE[1]})"
            doc_id = db.upsert_document(
                university_id=university["id"], collection=university["web_collection"],
                source_type="website", url=display_url, title=title, origin="seed",
                parent_url=CALENDAR_PAGE, extra='{"calendar_feed": "%s"}' % feed)
            results.append(_ingest_feed(doc_id, university, feed, display_url, title,
                                        academic_year, run_id, fetcher, _record_failure,
                                        index_blocks))
    return results


def _ingest_feed(doc_id, university, feed, display_url, title, academic_year, run_id, fetcher,
                 record_failure, index_blocks) -> dict[str, Any]:
    retrieved_at = db.now_iso()
    res = fetcher.fetch(feed, FEED_DOMAINS)
    if not res.ok:
        record_failure(doc_id, run_id, res.stage, res.error, feed)
        return {"url": display_url, "status": "failed", "error": res.error}
    rows = parse_rows(res.text)
    if not rows:
        msg = "لا توجد بنود منشورة في التقويم لهذا الفصل بعد"
        record_failure(doc_id, run_id, "extract", msg, feed)
        return {"url": display_url, "status": "failed", "error": msg}
    blocks = calendar_blocks(rows, title)
    h = content_hash("\n".join(b.text for b in blocks))
    prev = db.get_document(doc_id) or {}
    if prev.get("content_hash") == h and prev.get("status") == "indexed":
        db.update_document(doc_id, retrieved_at=retrieved_at, error=None)
        from .. import vectorstore
        vectorstore.update_document_metadata(university["web_collection"], doc_id,
                                             {"retrieved_at": retrieved_at})
        return {"url": display_url, "status": "unchanged"}
    stored, _ = index_blocks(doc_id, university, university["web_collection"], "website", title,
                             blocks, url=display_url, version="", academic_year=academic_year,
                             retrieved_at=retrieved_at, parent_url=CALENDAR_PAGE)
    db.update_document(doc_id, status="indexed", error=None, content_hash=h, title=title,
                       academic_year=academic_year, chunk_count=stored, retrieved_at=retrieved_at)
    return {"url": display_url, "status": "indexed", "chunks": stored, "rows": len(rows)}


def refresh_calendar_doc(doc: dict, university: dict, run_id: int | None,
                         fetcher: PoliteFetcher) -> dict[str, Any]:
    import json

    from .pipeline import _record_failure, index_blocks

    feed = json.loads(doc.get("extra") or "{}").get("calendar_feed", "")
    return _ingest_feed(doc["id"], university, feed, doc["url"], doc["title"],
                        doc["academic_year"], run_id, fetcher, _record_failure, index_blocks)
