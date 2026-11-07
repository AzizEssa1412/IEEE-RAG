"""Recognising structured course data (codes, titles, credits, requisites, semesters).

Used by both the PDF table extractor and the CSV/XLSX/JSON importers. Nothing is
invented: missing values stay empty, and requisite codes that do not match any course
in the same document are kept but reported as unresolved.
"""
from __future__ import annotations

import re
from typing import Any

from ..text.arabic import normalize_for_search

COURSE_CODE = re.compile(r"(?<!\d)(\d{7})(?!\d)")
_REQ_TOKEN = re.compile(r"\b([PC])\s*-\s*(\d{7})\b", re.IGNORECASE)
_NOTE = re.compile(r"see\s+note\s*(\d+)", re.IGNORECASE)

# Column synonyms (English + Arabic) used to map arbitrary spreadsheets / tables.
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "code": ["course no", "course #", "course code", "code", "course_number", "course number",
             "رقم المساق", "رمز المساق", "رقم المادة", "رمز المادة", "الرقم", "رقم المقرر"],
    "title": ["course name", "course title", "title", "name", "course_name",
              "اسم المساق", "اسم المادة", "المساق", "المادة", "اسم المقرر"],
    "credits": ["credits", "credit hours", "credit_hours", "cr", "cr.", "hours",
                "الساعات المعتمدة", "ساعات معتمدة", "عدد الساعات", "الساعات", "س.م"],
    "credits_theory": ["th", "theory", "نظري"],
    "credits_lab": ["lab", "practical", "عملي", "مختبر"],
    "prerequisites": ["prerequisites", "prerequisite", "prerequisites /corequisites",
                      "prerequisites/corequisites", "prereq", "prereqs", "pre-requisites",
                      "المتطلب السابق", "متطلب سابق", "المتطلبات السابقة", "المتطلبات"],
    "corequisites": ["corequisites", "corequisite", "coreq", "متطلب متزامن", "المتطلب المتزامن"],
    "department": ["department", "dept", "القسم", "الدائرة"],
    "program": ["program", "programme", "major", "البرنامج", "التخصص"],
    "semester": ["semester", "recommended semester", "term", "year", "الفصل", "الفصل المقترح",
                 "السنة", "السنة الدراسية"],
    "category": ["category", "requirement", "type", "نوع المتطلب", "المتطلب", "التصنيف"],
}


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", normalize_for_search(h or "").replace("\n", " ")).strip(" :")


def map_columns(headers: list[str | None]) -> dict[str, int]:
    """Map semantic field -> column index, using exact synonyms first, then containment."""
    mapping: dict[str, int] = {}
    normed = [_norm_header(h or "") for h in headers]
    for field, syns in COLUMN_SYNONYMS.items():
        syn_n = [_norm_header(s) for s in syns]
        for i, h in enumerate(normed):
            if h and h in syn_n and i not in mapping.values():
                mapping[field] = i
                break
    for field, syns in COLUMN_SYNONYMS.items():
        if field in mapping:
            continue
        syn_n = [_norm_header(s) for s in syns if len(s) > 3]
        for i, h in enumerate(normed):
            if h and i not in mapping.values() and any(s in h for s in syn_n):
                mapping[field] = i
                break
    return mapping


def parse_requisites(raw: str, notes: dict[str, str] | None = None,
                     default_kind: str = "prerequisite") -> list[dict[str, str]]:
    """'P-0702113, C-0702233' / 'See Note 1' / '3rd year +' -> structured requisites."""
    raw = (raw or "").replace("\n", " ").strip()
    if not raw or raw in {"---", "--", "-", "None", "لا يوجد", "لا شيء"}:
        return []
    reqs: list[dict[str, str]] = []
    for kind, code in _REQ_TOKEN.findall(raw):
        reqs.append({"kind": "prerequisite" if kind.upper() == "P" else "corequisite",
                     "code": code, "raw": raw})
    note = _NOTE.search(raw)
    if note and notes and note.group(1) in notes:
        note_text = notes[note.group(1)]
        for kind, code in _REQ_TOKEN.findall(note_text):
            reqs.append({"kind": "prerequisite" if kind.upper() == "P" else "corequisite",
                         "code": code, "raw": note_text})
        reqs.append({"kind": "condition", "code": "", "raw": note_text})
    if not reqs:
        bare = COURSE_CODE.findall(raw)
        if bare:
            reqs = [{"kind": default_kind, "code": c, "raw": raw} for c in bare]
        else:
            reqs = [{"kind": "condition", "code": "", "raw": raw}]
    seen, out = set(), []
    for r in reqs:
        key = (r["kind"], r["code"], r["raw"] if not r["code"] else "")
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def row_to_course(values: dict[str, Any], notes: dict[str, str] | None = None) -> dict | None:
    """Build a course dict from a {field: value} row. Returns None when there is no code."""
    code_raw = str(values.get("code") or "").strip()
    m = COURSE_CODE.search(code_raw)
    if not m:
        return None
    clean = {k: ("" if v is None else str(v).replace("\n", " ").strip()) for k, v in values.items()}
    th, lab = clean.get("credits_theory", ""), clean.get("credits_lab", "")
    credits = clean.get("credits", "")
    if not credits and (th or lab):
        nums = [int(x) for x in (th, lab) if x.isdigit()]
        credits = str(sum(nums)) if nums else ""
    reqs = parse_requisites(clean.get("prerequisites", ""), notes)
    reqs += parse_requisites(clean.get("corequisites", ""), notes, default_kind="corequisite")
    raw_req = " ; ".join(x for x in (clean.get("prerequisites", ""),
                                      clean.get("corequisites", "")) if x)
    return {
        "code": m.group(1),
        "title": clean.get("title", ""),
        "credits": credits,
        "credits_theory": th if th not in ("-",) else "",
        "credits_lab": lab if lab not in ("-",) else "",
        "department": clean.get("department", ""),
        "program": clean.get("program", ""),
        "category": clean.get("category", ""),
        "semester": clean.get("semester", ""),
        "requisites_raw": raw_req,
        "requisites": reqs,
        "page": int(values.get("page") or 0),
    }


def course_passage(c: dict[str, Any]) -> str:
    """Human-readable Arabic/English passage describing one course row (for retrieval)."""
    parts = [f"المساق {c['code']}"]
    if c.get("title"):
        parts.append(f"({c['title']})")
    if c.get("credits"):
        parts.append(f"— الساعات المعتمدة: {c['credits']}")
    if c.get("requisites_raw"):
        parts.append(f"— المتطلبات: {c['requisites_raw']}")
    for key, label in (("department", "القسم"), ("program", "البرنامج"),
                       ("category", "التصنيف"), ("semester", "الفصل المقترح")):
        if c.get(key):
            parts.append(f"— {label}: {c[key]}")
    return " ".join(parts)
