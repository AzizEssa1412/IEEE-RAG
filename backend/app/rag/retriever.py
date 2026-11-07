"""Hybrid retrieval scoped to ONE university.

dense (multilingual-e5, cosine)  +  lexical BM25 on normalised Arabic tokens
-> reciprocal-rank fusion -> evidence gate.

The evidence gate decides whether the retrieved passages actually support an answer;
if not, the assistant must say the official sources do not contain it.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .. import db, vectorstore
from ..config import COLLECTION_HACKATHON, settings
from ..ingest.courses import COURSE_CODE
from ..text.arabic import expand_query, normalize_for_search, tokenize
from .evidence import content_terms, missing_terms

DENSE_WEIGHT = 1.0
LEXICAL_WEIGHT = 0.6
DENSE_TOP_GUARANTEE = 3
SHORT_CHUNK = 120
EVIDENCE_POOL = 10


@dataclass
class Passage:
    id: str
    text: str
    meta: dict[str, Any]
    collection: str
    similarity: float = 0.0
    bm25: float = 0.0
    score: float = 0.0
    kind: str = "chunk"           # chunk | course_record
    extra: dict[str, Any] = field(default_factory=dict)


def collections_for(university: dict) -> list[str]:
    return [c for c in (university["web_collection"], university["uploads_collection"],
                        COLLECTION_HACKATHON) if c]


# --- BM25 ---------------------------------------------------------------------------
class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.lens = [len(d) for d in docs]
        self.avg = sum(self.lens) / len(docs) if docs else 0.0
        self.tf = [Counter(d) for d in docs]
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for tf, ln in zip(self.tf, self.lens):
            s = 0.0
            for t in query:
                if t not in tf:
                    continue
                f = tf[t]
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / (
                    f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out


_cache_lock = threading.Lock()
_cache: dict[str, tuple[int, list[dict], BM25]] = {}


def _lexical_index(university: dict) -> tuple[list[dict], BM25]:
    key = university["id"]
    ver = vectorstore.data_version()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == ver:
            return hit[1], hit[2]
        chunks = vectorstore.all_chunks(collections_for(university), university["id"])
        index = BM25([tokenize(f"{c['meta'].get('title', '')} {c['meta'].get('section', '')} "
                               f"{c['text']}") for c in chunks])
        _cache[key] = (ver, chunks, index)
        return chunks, index


# --- Main entry ---------------------------------------------------------------------
@dataclass
class Retrieval:
    passages: list[Passage]
    has_evidence: bool
    best_similarity: float
    expanded_query: str
    missing: list[str] = field(default_factory=list)   # question words no passage covers


def retrieve(question: str, university: dict, k: int | None = None) -> Retrieval:
    k = k or settings.top_k
    expanded = expand_query(question)
    cols = collections_for(university)

    dense = vectorstore.vector_search(cols, university["id"], expanded, k=25)
    chunks, bm25 = _lexical_index(university)
    q_tokens = tokenize(expanded)
    lex_scores = bm25.scores(q_tokens) if chunks else []
    lex_ranked = sorted(range(len(chunks)), key=lambda i: -lex_scores[i])[:25]

    # Weighted reciprocal-rank fusion: semantic matches lead, lexical matches support.
    fused: dict[str, Passage] = {}
    for rank, h in enumerate(dense):
        p = fused.setdefault(h["id"], Passage(h["id"], h["text"], h["meta"], h["collection"]))
        p.similarity = h["similarity"]
        p.score += DENSE_WEIGHT / (60 + rank)
        # Cross-lingual sources (e.g. an English study plan for an Arabic question) can only be
        # found semantically; make sure the strongest semantic hits are never crowded out.
        if rank < DENSE_TOP_GUARANTEE and len(h["text"]) >= SHORT_CHUNK:
            p.score += DENSE_WEIGHT / 60
    for rank, i in enumerate(lex_ranked):
        if lex_scores[i] <= 0:
            break
        c = chunks[i]
        p = fused.setdefault(c["id"], Passage(c["id"], c["text"], c["meta"], c["collection"]))
        p.bm25 = lex_scores[i]
        p.score += LEXICAL_WEIGHT / (60 + rank)

    # Exact course-code matches are strong evidence (e.g. "0702211").
    codes = set(COURSE_CODE.findall(question))
    for p in fused.values():
        if codes and any(c in p.text for c in codes):
            p.score += 0.05

    # Fragments (a heading, one stray line) embed mostly their page title: they look similar
    # to everything on that page but rarely contain an answer.
    for p in fused.values():
        if len(p.text) < SHORT_CHUNK:
            p.score *= 0.5

    ranked = sorted(fused.values(), key=lambda p: -p.score)

    # Structured course records (with their original document + page) for mentioned codes,
    # or for courses named in the question (Arabic names reach English titles via the glossary).
    named = codes or _codes_named_in(expanded, university)
    records: list[Passage] = []
    for code in sorted(named):
        for row in db.course_with_relations(university["id"], code):
            records.append(_course_record_passage(university, row))
    if not named:
        records += _plan_records(question, expanded, university)
    records += _program_records(question, university)

    best_sim = max((p.similarity for p in ranked), default=0.0)

    # Answerability: similarity only shows the passages are in the right area; one passage must
    # also account for every meaningful word of the question (see evidence.py).
    covered: list[Passage] = []
    missing: list[str] | None = None
    # A question made only of filler words («كم ساعة بقدر أسجل؟», «شو بصير إذا رسبت بمادة؟»)
    # names no subject a passage could prove; only a structured record triggered by
    # something specific (a course code / name, a plan request) can answer it.
    specific = bool(content_terms(question))
    for p in records + ranked[:EVIDENCE_POOL]:
        if p.kind == "chunk" and not specific:
            continue
        miss = missing_terms(question, p.text)
        if not miss:
            p.extra["covered"] = True
            covered.append(p)
        elif missing is None or len(miss) < len(missing):
            missing = miss
    in_area = bool(records) or best_sim >= settings.min_similarity - 0.03
    has_evidence = bool(covered) and in_area

    others = [p for p in records + ranked
              if p not in covered and (p.kind != "chunk" or p.similarity >= settings.min_similarity - 0.06
                                       or p.bm25 > 0)]
    selected = (covered + others)[: max(k, len(covered))]
    return Retrieval(passages=selected, has_evidence=has_evidence, best_similarity=best_sim,
                     expanded_query=expanded, missing=[] if covered else (missing or []))


def _course_record_passage(university: dict, row: dict) -> Passage:
    reqs = []
    for r in row["requisites"]:
        label = {"prerequisite": "متطلب سابق", "corequisite": "متطلب متزامن",
                 "condition": "شرط"}.get(r["kind"], r["kind"])
        if r["required_code"]:
            note = "" if r.get("resolved", True) else \
                " (تنبيه: هذا الرقم غير موجود ضمن مساقات الوثيقة نفسها)"
            reqs.append(f"{label}: {r['required_code']}{note}")
        else:
            reqs.append(f"{label}: {r['raw']}")
    dependents = ", ".join(f"{d['code']} {d['title']}" for d in row["required_by"])
    lines = [f"سجل المساق {row['code']} — {row['title']}",
             f"الساعات المعتمدة: {row['credits'] or 'غير مذكور'}"]
    if row["category"]:
        lines.append(f"التصنيف في الوثيقة: {row['category']}")
    if row["semester"]:
        lines.append(f"الفصل المقترح في الخطة: {row['semester']}")
    lines.append("المتطلبات: " + ("; ".join(reqs) if reqs else "لا يوجد متطلب مذكور"))
    if dependents:
        lines.append(f"مساقات تعتمد عليه كمتطلب: {dependents}")
    if row.get("description"):
        lines.append(f"وصف المساق كما ورد في الوثيقة: {row['description']}")
    meta = {
        "university_id": university["id"], "university_name": university["name_ar"],
        "collection": row["collection"], "source_type": row["source_type"],
        "doc_id": row["document_id"], "title": row["document_title"],
        "url": row["document_url"] or "", "file_ref": row["file_ref"] or "",
        "page": row["page"], "section": row["category"], "article": "",
        "version": row["version"], "retrieved_at": row["retrieved_at"] or "",
        "academic_year": "",
    }
    return Passage(id=f"course-{row['id']}", text="\n".join(lines), meta=meta,
                   collection=row["collection"], similarity=1.0, kind="course_record")


_GENERIC_TITLES = {"special topics", "project", "university requirement",
                   "free elective", "department elective", "faculty elective"}


def _codes_named_in(expanded: str, university: dict, limit: int = 3) -> set[str]:
    """Courses whose full official title appears in the (glossary-expanded) question."""
    q = " " + re.sub(r"[^\w&]+", " ", expanded.lower()) + " "
    hits: dict[str, int] = {}
    for r in db.query("SELECT DISTINCT code, title FROM courses WHERE university_id=?",
                      (university["id"],)):
        t = re.sub(r"[^\w&]+", " ", (r["title"] or "").lower()).strip()
        if len(t) >= 6 and t not in _GENERIC_TITLES and f" {t} " in q:
            hits[r["code"]] = max(hits.get(r["code"], 0), len(t))
    # the longest matching titles are the most specific ones
    best = sorted(hits, key=lambda c: -hits[c])[:limit]
    return set(best)


_YEARS = ("First", "Second", "Third", "Fourth", "Fifth", "Sixth")
_PLAN_WORDS = ("مواد", "مساقات", "خطه", "courses", "plan", "program of study", "المطلوبه")


def _plan_records(question: str, expanded: str, university: dict) -> list[Passage]:
    """Study-plan questions about a named program -> lists built from the extracted courses.

    Only answers with what the document's tables contain: semester distribution, elective
    or compulsory course tables, or the whole plan grouped by requirement table.
    """
    ql = expanded.lower()
    qn = normalize_for_search(question)
    programs = db.query(
        """SELECT DISTINCT c.program, c.document_id FROM courses c
           JOIN documents d ON d.id = c.document_id
           WHERE c.university_id=? AND c.program != '' AND d.status='indexed'""",
        (university["id"],))
    out: list[Passage] = []
    only_one = len({p["program"] for p in programs}) == 1
    for prog in programs:
        core = re.sub(r"^(b\.?\s*sc\.?|bachelor of|bachelor|بكالوريوس)\s*(in\s+)?", "",
                      prog["program"].strip(), flags=re.IGNORECASE).strip().lower()
        # With a single study plan loaded, plan questions need not repeat the program name.
        if not only_one and (len(core) < 4 or core not in ql):
            continue
        rows = db.query("SELECT * FROM courses WHERE document_id=? AND program=? ORDER BY id",
                        (prog["document_id"], prog["program"]))
        year = next((y for y in _YEARS if f"{y.lower()} year" in ql), "")
        sem = "Fall" if "fall semester" in ql else "Spring" if "spring semester" in ql else ""
        if year or sem:
            sel = [r for r in rows if r["semester"] and (not year or r["semester"].startswith(year))
                   and (not sem or sem in r["semester"])]
            heading = "توزيع المساقات المقترح في الخطة"
            group_by = "semester"
        elif "elective" in ql:
            sel = [r for r in rows if "elective" in (r["category"] or "").lower()]
            heading, group_by = "المساقات الاختيارية في الخطة", "category"
        elif "compulsory" in ql and "elective" not in ql and "المطلوب" not in qn:
            sel = [r for r in rows if "compulsory" in (r["category"] or "").lower()]
            heading, group_by = "المساقات الإجبارية في الخطة", "category"
        elif any(w in qn or w in ql for w in _PLAN_WORDS):
            sel, heading, group_by = rows, "مساقات الخطة الدراسية حسب جداول المتطلبات", "category"
        else:
            continue
        if not sel:
            continue
        doc = db.get_document(prog["document_id"]) or {}
        lines = [f"{heading} — {prog['program']} ({doc.get('title', '')})"]
        groups: dict[str, list[dict]] = {}
        for r in sel:
            groups.setdefault(r[group_by] or "غير مصنف", []).append(r)
        for g, items in groups.items():
            total = sum(int(r["credits"]) for r in items if str(r["credits"]).isdigit())
            lines.append(f"\n{g} ({len(items)} مساق، {total} ساعة معتمدة):")
            lines += [f"• {r['code']} {r['title']} — {r['credits'] or '?'} ساعات" for r in items]
        pages = sorted({r["page"] for r in sel if r["page"]})
        meta = {"university_id": university["id"], "university_name": university["name_ar"],
                "collection": doc.get("collection", ""), "source_type": doc.get("source_type", ""),
                "doc_id": doc.get("id", 0), "title": doc.get("title", ""), "url": doc.get("url") or "",
                "file_ref": doc.get("file_ref") or "", "page": pages[0] if len(pages) == 1 else 0,
                "section": heading, "article": "", "version": doc.get("version") or "",
                "retrieved_at": doc.get("retrieved_at") or "", "academic_year": ""}
        out.append(Passage(id=f"plan-{prog['document_id']}-{group_by}", text="\n".join(lines),
                           meta=meta, collection=meta["collection"], similarity=1.0,
                           kind="plan_record"))
    return out[:2]


def _program_records(question: str, university: dict, limit: int = 3) -> list[Passage]:
    """Programs explicitly named in an ADMISSION question -> their official table row."""
    qn = normalize_for_search(question)
    if not any(t in qn for t in ("معدل", "قبول", "التحاق", "حد ادني", "فرع", "مكان دراسه")):
        return []
    rows = db.query(
        """SELECT p.*, d.title AS document_title, d.url AS document_url, d.version,
                  d.retrieved_at, d.collection, d.source_type, d.id AS doc_id
           FROM programs p JOIN documents d ON d.id=p.document_id WHERE p.university_id=?""",
        (university["id"],))
    out: list[Passage] = []
    for r in rows:
        base = normalize_for_search(re.split(r"[(:،,]", r["name"])[0]).strip()
        if len(base) < 5 or base not in qn:
            continue
        text = "\n".join(x for x in [
            f"البرنامج: {r['name']}",
            f"الكلية: {r['faculty']}" if r["faculty"] else "",
            f"مكان الدراسة: {r['campus']}" if r["campus"] else "",
            f"الحد الأدنى للمعدل: {r['min_average']}" if r["min_average"] else "",
            f"فرع الثانوية العامة: {r['streams']}" if r["streams"] else "",
            f"الدرجة: {r['degree']}" if r["degree"] else "",
            f"العام الأكاديمي: {r['academic_year']}" if r["academic_year"] else "",
        ] if x)
        meta = {"university_id": university["id"], "university_name": university["name_ar"],
                "collection": r["collection"], "source_type": r["source_type"],
                "doc_id": r["doc_id"], "title": r["document_title"],
                "url": r["document_url"] or "", "file_ref": "", "page": 0,
                "section": "جدول البرامج المطروحة", "article": "", "version": r["version"],
                "retrieved_at": r["retrieved_at"] or "", "academic_year": r["academic_year"]}
        out.append(Passage(id=f"program-{r['id']}", text=text, meta=meta,
                           collection=r["collection"], similarity=1.0, kind="program_record"))
        if len(out) >= limit:
            break
    return out
