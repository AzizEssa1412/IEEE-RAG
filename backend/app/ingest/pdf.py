"""PDF extraction with PyMuPDF: page-accurate text, tables, headings and course data.

* every Block keeps its 1-based page number -> citations point at the real page
* tables are extracted as self-describing rows and their area is removed from the
  running text so nothing is indexed twice
* Arabic presentation forms are folded (NFKC) and visually-reversed lines repaired
* scanned PDFs without a text layer are reported as failures (no silent OCR guessing)
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from statistics import median

import fitz  # PyMuPDF

from ..text.arabic import clean_text, find_academic_year, fix_reversed_arabic_line
from ..text.chunking import Block
from .courses import COURSE_CODE, map_columns, row_to_course

_MONTH_YEAR = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\s+(19|20)\d{2}\b", re.IGNORECASE)
_TABLE_LABEL = re.compile(r"Table\s+[IVX]+-[a-z]\b|جدول\s*\(?[\d٠-٩]+\)?", re.IGNORECASE)
_YEAR_LABEL = re.compile(r"(First|Second|Third|Fourth|Fifth|Sixth)\s+Year(\s*\([^)]*\))?"
                         r"|السنة\s+(الأولى|الاولى|الثانية|الثالثة|الرابعة|الخامسة)", re.IGNORECASE)
_NOTE_LINE = re.compile(r"Note\s*(\d+)\s*:\s*(.+)", re.IGNORECASE)
_SEMESTER_GROUP = re.compile(r"\b(fall|spring|summer)\b|الفصل\s+(الأول|الاول|الثاني|الصيفي)", re.IGNORECASE)
_REQ_KIND = re.compile(r"\b(compulsory|electives?|free)\b|إجباري|اجباري|اختياري", re.IGNORECASE)


@dataclass
class PdfContent:
    title: str
    blocks: list[Block]
    page_count: int
    metadata: dict
    courses: list[dict] = field(default_factory=list)
    version: str = ""
    academic_year: str = ""
    text_pages: int = 0

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


def _pdf_date(raw: str) -> str:
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _clean_cell(v) -> str:
    if v in (None, ""):
        return ""
    lines = [fix_reversed_arabic_line(x) for x in clean_text(str(v)).splitlines()]
    return "\n".join(lines)


def _inside(bbox, rect) -> bool:
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return rect.x0 - 1 <= cx <= rect.x1 + 1 and rect.y0 - 1 <= cy <= rect.y1 + 1


def _label_above(lines: list[tuple[float, str]], top: float, pattern: re.Pattern) -> str:
    best = ""
    for y, text in lines:
        if y < top:
            m = pattern.search(text)
            if m:
                best = m.group(0)
    return best


def _table_to_rows(table) -> tuple[list[str], list[list[str]], list[int]]:
    """Return (column names, data rows, kept column indices) honouring merged cells."""
    raw = [[_clean_cell(c) for c in row] for row in table.extract()]
    if not raw:
        return [], [], []
    width = max(len(r) for r in raw)
    raw = [r + [""] * (width - len(r)) for r in raw]
    header = raw[0]
    body = raw[1:]
    sub = None
    if body:
        first = body[0]
        fills_gaps = any(c and not header[i] for i, c in enumerate(first))
        no_numbers = not any(re.search(r"\d", c) for c in first)
        if not any(COURSE_CODE.search(c) for c in first) and no_numbers and (
                fills_gaps or any(c.lower() in ("th", "lab", "نظري", "عملي") for c in first)):
            sub, body = first, body[1:]
    names, keep = [], []
    group = ""
    for i in range(width):
        if header[i]:
            group = header[i] if _SEMESTER_GROUP.search(header[i]) else ""
        name = header[i] or (sub[i] if sub else "")
        if sub and sub[i] and header[i]:
            name = sub[i]
        if sub and sub[i] and group:
            # "Fall Semester" / "Spring Semester" spans several columns: keep it in each name
            name = f"{group}: {sub[i]}"
        if not name and not any(r[i] for r in body):
            continue
        if not name and keep and not any(r[i] for r in body):
            continue
        names.append(name)
        keep.append(i)
    rows = [[r[i] for i in keep] for r in body if any(r[i] for i in keep)]
    # Drop columns that are entirely empty in the body (merged-cell artefacts).
    used = [j for j in range(len(keep)) if any(row[j] for row in rows) or names[j]]
    names = [names[j] for j in used]
    rows = [[row[j] for j in used] for row in rows]
    keep = [keep[j] for j in used]
    return names, rows, keep


def _format_row(names: list[str], row: list[str]) -> str:
    pairs = [f"{n}: {v}" if n else v for n, v in zip(names, row) if v]
    return " | ".join(pairs)


def _semester_plan_rows(names: list[str], rows: list[list[str]], year_label: str,
                        page: int) -> list[tuple[str, str]]:
    """Side-by-side 'Fall Semester | Spring Semester' plan tables -> (code, semester)."""
    joined = " ".join(names).lower()
    if "fall" not in joined and "spring" not in joined and "الأول" not in joined:
        return []
    split = next((i for i, n in enumerate(names) if "spring" in n.lower() or "الثاني" in n), None)
    out = []
    for row in rows:
        halves = [(row[:split], "Fall Semester"), (row[split:], "Spring Semester")] \
            if split else [(row, "")]
        for half, sem in halves:
            for cell in half:
                m = COURSE_CODE.fullmatch(cell.strip())
                if m:
                    out.append((m.group(1), f"{year_label} – {sem}".strip(" –")))
                    break
    return out


def extract_pdf(source: str | bytes) -> PdfContent:
    doc = fitz.open(stream=source, filetype="pdf") if isinstance(source, bytes) else fitz.open(source)
    try:
        meta = doc.metadata or {}
        blocks: list[Block] = []
        courses: dict[tuple[str, str], dict] = {}
        semesters: dict[str, str] = {}
        section = ""
        req_kind = ""
        text_pages = 0
        sizes = []
        for page in doc:
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for span in line["spans"]:
                        if span["text"].strip():
                            sizes.append(round(span["size"], 1))
        body_size = median(sizes) if sizes else 11.0

        # Pre-pass: collect lines per page so running headers/footers can be detected.
        pages_data = []
        for page in doc:
            try:
                tables = page.find_tables().tables
            except Exception:  # pragma: no cover - table finder is best effort
                tables = []
            rects = [fitz.Rect(t.bbox) for t in tables]
            # Extract rows NOW: table objects are only valid while their page is alive.
            tables = [(tuple(t.bbox), _table_to_rows(t)) for t in tables]
            raw_lines: list[tuple[float, str, bool]] = []
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not text or re.fullmatch(r"[\d٠-٩]{1,3}", text):  # page numbers
                        continue
                    if any(_inside(line["bbox"], r) for r in rects):
                        continue
                    size = max(s["size"] for s in line["spans"])
                    bold = all((s["flags"] & 16) or "Bold" in s["font"] for s in line["spans"]
                               if s["text"].strip())
                    is_heading = (size >= body_size * 1.18 or (bold and len(text) < 90)) \
                        and not text.rstrip().endswith((".", "،"))
                    text = fix_reversed_word_order(fix_reversed_arabic_line(clean_text(text)))
                    raw_lines.append((line["bbox"][1], text, is_heading))
            pages_data.append((tables, raw_lines))
        repeated = _running_lines([[t for _, t, _ in lines] for _, lines in pages_data])

        for pno, (page, (tables, raw_lines)) in enumerate(zip(doc, pages_data), start=1):
            line_items = [li for li in raw_lines if _line_key(li[1]) not in repeated]
            if raw_lines:
                text_pages += 1
            notes = {m.group(1): m.group(2).strip()
                     for _, t, _ in line_items for m in [_NOTE_LINE.search(t)] if m}
            all_lines = [(y, t) for y, t, _ in line_items]

            # merge consecutive non-heading lines into paragraphs
            para: list[str] = []

            def flush_para() -> None:
                if para:
                    blocks.append(Block(clean_text(" ".join(para)), "para", pno, section))
                    para.clear()

            items = sorted(line_items, key=lambda x: x[0])
            # interleave tables at their vertical position
            table_slots = sorted(((t[0][1], t) for t in tables), key=lambda x: x[0])
            ti = 0
            for y, text, is_heading in items:
                while ti < len(table_slots) and table_slots[ti][0] < y:
                    flush_para()
                    _emit_table(table_slots[ti][1], all_lines, pno, section, blocks, courses,
                                semesters, notes, req_kind)
                    ti += 1
                if is_heading and len(text) > 1:
                    flush_para()
                    section = text[:200]
                    if _REQ_KIND.search(text):
                        req_kind = re.sub(r"\s*Table\s+[IVX]+-[a-z]\s*$", "", text[:80]).strip()
                    blocks.append(Block(text, "heading", pno, section))
                else:
                    para.append(text)
                    if text.endswith((".", ":", "؛")) and sum(len(p) for p in para) > 300:
                        flush_para()
            flush_para()
            while ti < len(table_slots):
                _emit_table(table_slots[ti][1], all_lines, pno, section, blocks, courses,
                            semesters, notes, req_kind)
                ti += 1

        for co in courses.values():
            if not co.get("semester") and co["code"] in semesters:
                co["semester"] = semesters[co["code"]]
        _attach_descriptions(blocks, list(courses.values()))

        first_pages = "\n".join(p.get_text() for p in list(doc)[:2])
        mv = _MONTH_YEAR.search(first_pages)
        version = mv.group(0) if mv else ""
        if not version and meta.get("modDate"):
            version = f"PDF {_pdf_date(meta['modDate'])}"
        title = (meta.get("title") or "").strip()
        return PdfContent(
            title=title, blocks=[b for b in blocks if b.text.strip()], page_count=doc.page_count,
            metadata=meta, courses=list(courses.values()), version=version,
            academic_year=find_academic_year(first_pages), text_pages=text_pages,
        )
    finally:
        doc.close()


# "0702451 Operating Systems (3 credits)" / "Internship (3 credits)" start a course description;
# a bullet followed by a course code also starts one (some entries omit the credits).
_DESC_START = re.compile(
    r"(?:(?<=^)|(?<=[\s♦◆•]))(?:(\d{7})[ \t]+)?"
    r"([A-Z][A-Za-z0-9&,'/.\- ]{2,90}?)\s*\(\s*\d+\s*credits?\s*\)", re.IGNORECASE)
_DESC_BULLET = re.compile(r"[♦◆]\s*(\d{7})\s+([A-Z][^\n(]{2,90})")


def _title_key(title: str) -> str:
    t = re.sub(r"\blaboratory\b", "lab", (title or "").lower())
    return re.sub(r"[^a-z0-9]+", "", t)


def _attach_descriptions(blocks: list[Block], courses: list[dict]) -> None:
    """Attach each course's catalogue description (text as written in the document)."""
    parts, offsets = [], []
    pos = 0
    for b in blocks:
        if b.kind == "table":
            continue
        offsets.append((pos, b.page))
        parts.append(b.text)
        pos += len(b.text) + 1
    full = "\n".join(parts)
    starts: dict[int, tuple[str, str]] = {}
    for m in _DESC_START.finditer(full):
        starts[m.start()] = (m.group(1) or "", m.group(2).strip())
    for m in _DESC_BULLET.finditer(full):
        if not any(abs(m.start() - s) < 4 for s in starts):
            starts[m.start()] = (m.group(1), m.group(2).strip())
    order = sorted(starts)
    by_code: dict[str, tuple[str, int]] = {}
    by_title: dict[str, tuple[str, int]] = {}
    for i, s in enumerate(order):
        end = order[i + 1] if i + 1 < len(order) else len(full)
        body = re.sub(r"\s+", " ", full[s:end]).strip(" ♦◆•")
        page = next((p for o, p in reversed(offsets) if o <= s), 0)
        code, title = starts[s]
        if len(body) < 40:
            continue
        if code:
            by_code.setdefault(code, (body[:1500], page))
        by_title.setdefault(_title_key(title), (body[:1500], page))
    used: set[str] = set()
    pending = []
    for co in courses:
        key = _title_key(co.get("title", ""))
        hit = by_code.get(co["code"]) or by_title.get(key)
        if hit:
            co["description"], co["description_page"] = hit
            used.add(_title_key(hit[0].split("(")[0]))
        elif key:
            pending.append((co, key))
    # Catalogues misspell their own titles («Mathmetics», «ElectronicsI»): close matches only,
    # best pairs first, and each description is given to one course at most.
    pairs = sorted(((SequenceMatcher(None, key, t).ratio(), id(co), co, t)
                    for co, key in pending for t in by_title if t not in used),
                   key=lambda x: -x[0])
    done: set[int] = set()
    for ratio, cid, co, t in pairs:
        if ratio < 0.8:
            break
        if cid in done or t in used:
            continue
        co["description"], co["description_page"] = by_title[t]
        done.add(cid)
        used.add(t)


def _line_key(text: str) -> str:
    """Normalise a line for header/footer detection (page numbers and spacing ignored)."""
    return re.sub(r"[\d٠-٩\s]+", "", text.lower())


def _running_lines(pages: list[list[str]]) -> set[str]:
    """Short lines that repeat on many pages are running headers/footers, not knowledge."""
    n = len(pages)
    if n < 4:
        return set()
    counts: dict[str, int] = {}
    for lines in pages:
        for key in {_line_key(t) for t in lines if len(t) < 90}:
            if key:
                counts[key] = counts.get(key, 0) + 1
    threshold = max(3, int(n * 0.3))
    return {k for k, c in counts.items() if c >= threshold}


_PUNCT_START = re.compile(r"^[.،:؛!؟)]")
_PUNCT_END = re.compile(r"[.،:؛!؟(]$")


def fix_reversed_word_order(line: str) -> str:
    """Some PDFs yield Arabic words in visual (reversed) order: '.المعادلة الملف ...'.

    Punctuation then sits at the START of words. If that happens more often than at the
    end of words, the word order is reversed back. Character order inside words is kept.
    """
    words = line.split()
    if len(words) < 3 or not re.search(r"[؀-ۿ]", line):
        return line
    starts = sum(1 for w in words if _PUNCT_START.match(w) and re.search(r"[ء-ي]", w))
    ends = sum(1 for w in words if _PUNCT_END.search(w))
    if starts > ends:
        fixed = []
        for w in reversed(words):
            m = _PUNCT_START.match(w)
            fixed.append(w[1:] + m.group(0) if m and len(w) > 1 else w)
        return " ".join(fixed)
    return line


def _split_multi_course_rows(rows: list[list[str]]) -> list[list[str]]:
    """A cell holding two course codes (one per line) with one name per line is two rows."""
    out = []
    for row in rows:
        counts = [len(COURSE_CODE.findall(c)) for c in row]
        n = max(counts, default=0)
        code_col = counts.index(n) if n else -1
        if n >= 2 and len(row[code_col].split("\n")) == n:
            parts = [c.split("\n") for c in row]
            out += [[p[k] if len(p) == n else ("" if len(p) > 1 else p[0]) for p in parts]
                    for k in range(n)]
        else:
            out.append(row)
    return out


def _emit_table(table, page_lines, pno, section, blocks, courses, semesters, notes,
                req_kind: str = "") -> None:
    bbox, (names, rows, _) = table
    if not rows:
        return
    rows = _split_multi_course_rows(rows)
    top = bbox[1]
    category = _label_above(page_lines, top, _TABLE_LABEL)
    year_label = _label_above(page_lines, top, _YEAR_LABEL)
    sec = section or category
    if any(names):
        blocks.append(Block(" | ".join(n for n in names if n), "heading", pno, sec))
    for row in rows:
        text = _format_row(names, row)
        if text:
            if year_label and not any("Year" in x for x in row):
                text = f"{year_label}: {text}"
            blocks.append(Block(text, "table", pno, sec))

    plan = _semester_plan_rows(names, rows, year_label, pno)
    if plan:
        for code, sem in plan:
            semesters.setdefault(code, sem)
        return

    mapping = map_columns(names)
    if "code" not in mapping:
        return
    for row in rows:
        values = {f: row[i] if i < len(row) else "" for f, i in mapping.items()}
        cat = values.get("category") or category
        if cat and req_kind and req_kind.lower() not in cat.lower():
            cat = f"{cat} – {req_kind}"
        values["category"] = cat
        values["page"] = pno
        course = row_to_course(values, notes)
        if course:
            courses.setdefault((course["code"], course["category"]), course)
