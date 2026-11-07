"""Command-line ingestion (same code paths as the admin dashboard).

Examples (from backend/):
  python -m scripts.ingest website --university alquds --max-pages 40 --max-pdfs 20
  python -m scripts.ingest upload ../data/uploads/AcademicPlan-AQU-Eng-CE2010.pdf \
      --university alquds --program "B.Sc. Computer Engineering"
  python -m scripts.ingest report
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Arabic output on Windows consoles

from app import db  # noqa: E402
from app.config import COLLECTION_HACKATHON, settings  # noqa: E402
from app.ingest import pipeline  # noqa: E402


def cmd_website(a: argparse.Namespace) -> None:
    res = pipeline.run_website_ingestion(a.university, discover=not a.no_discover,
                                         max_pages=a.max_pages, max_pdfs=a.max_pdfs)
    for r in res["results"]:
        line = f"[{r['status']:>9}] {r['url']}"
        if r.get("chunks"):
            line += f"  ({r['chunks']} مقطع)"
        if r.get("error"):
            line += f"  — {r['error']}"
        print(line)
    print(json.dumps({k: v for k, v in res.items() if k != "results"}, ensure_ascii=False))


def cmd_upload(a: argparse.Namespace) -> None:
    uni = db.get_university(a.university)
    collection = COLLECTION_HACKATHON if a.hackathon else uni["uploads_collection"]
    p = Path(a.file)
    res = pipeline.import_upload(p, p.name, a.university, collection, title=a.title,
                                 version=a.version, academic_year=a.academic_year,
                                 program=a.program)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_report(_: argparse.Namespace) -> None:
    for d in db.query("SELECT id, status, source_type, collection, chunk_count, title, url, "
                      "file_ref, error FROM documents ORDER BY status, id"):
        print(f"#{d['id']:<4} {d['status']:<9} {d['source_type']:<8} {d['chunk_count']:>4}  "
              f"{d['title'][:60]}  {d['url'] or d['file_ref']}"
              + (f"\n       ↳ {d['error']}" if d["error"] else ""))


def cmd_reindex(a) -> None:
    """Re-embed every indexed document (after changing EMBEDDING_MODEL). Stop the server first."""
    from app import db
    from app.ingest import pipeline

    ids = [r["id"] for r in db.query(
        "SELECT id FROM documents WHERE status IN ('indexed','pending','failed')")]
    print(f"re-indexing {len(ids)} documents with {settings.embedding_model} …")
    res = pipeline.refresh_documents(ids)
    print({k: v for k, v in res.items() if k != "results"})


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("website")
    w.add_argument("--university", default="alquds")
    w.add_argument("--max-pages", type=int)
    w.add_argument("--max-pdfs", type=int)
    w.add_argument("--no-discover", action="store_true")
    u = sub.add_parser("upload")
    u.add_argument("file")
    u.add_argument("--university", default="alquds")
    u.add_argument("--hackathon", action="store_true",
                   help="store in hackathon_official_documents instead of uploads")
    u.add_argument("--title", default="")
    u.add_argument("--version", default="")
    u.add_argument("--academic-year", default="")
    u.add_argument("--program", default="")
    sub.add_parser("report")
    sub.add_parser("reindex", help="re-embed all indexed documents (after a model change)")
    a = ap.parse_args()
    {"website": cmd_website, "upload": cmd_upload, "report": cmd_report,
     "reindex": cmd_reindex}[a.cmd](a)


if __name__ == "__main__":
    main()
