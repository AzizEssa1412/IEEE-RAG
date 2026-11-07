"""File importers: PDF, TXT, CSV, JSON, XLSX -> Blocks (+ structured course rows).

Every block keeps a precise location (PDF page, or sheet/row for tabular files) so
answers can cite it. Course rows are recognised via column synonyms; unrecognised
tables are still indexed as self-describing rows. Nothing is guessed or filled in.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..text.arabic import clean_text, find_academic_year
from ..text.chunking import Block
from .courses import course_passage, map_columns, row_to_course

SUPPORTED = {".pdf", ".txt", ".md", ".csv", ".json", ".xlsx"}


@dataclass
class ImportedContent:
    title: str
    source_type: str              # pdf | dataset | text
    blocks: list[Block]
    courses: list[dict] = field(default_factory=list)
    version: str = ""
    academic_year: str = ""
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


class ImportError_(Exception):
    """Raised with an Arabic, user-facing message when a file cannot be imported."""


def import_file(path: Path, original_name: str | None = None) -> ImportedContent:
    name = original_name or path.name
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED:
        raise ImportError_(f"نوع الملف غير مدعوم: {ext} (المدعوم: PDF, TXT, CSV, JSON, XLSX)")
    data = path.read_bytes()
    if not data:
        raise ImportError_("الملف فارغ")
    if ext == ".pdf":
        return _import_pdf(data, name)
    if ext in (".txt", ".md"):
        return _import_text(_decode(data), name)
    if ext == ".csv":
        return _import_rows(_csv_rows(_decode(data)), name, sheet="CSV")
    if ext == ".json":
        return _import_json(_decode(data), name)
    return _import_xlsx(data, name)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_("تعذّر قراءة ترميز الملف (يُفضّل UTF-8)")


def _import_pdf(data: bytes, name: str) -> ImportedContent:
    from .pdf import extract_pdf

    try:
        pc = extract_pdf(data)
    except Exception as e:  # noqa: BLE001 - surface the parser error to the admin
        raise ImportError_(f"تعذّر فتح ملف PDF: {e}") from e
    if pc.text_pages == 0 or len(pc.text.strip()) < 50:
        raise ImportError_("لا يحتوي ملف PDF على نص قابل للاستخراج (غالباً ممسوح ضوئياً؛ "
                           "التعرف الضوئي OCR غير مفعّل)")
    warnings = []
    if pc.text_pages < pc.page_count:
        warnings.append(f"{pc.page_count - pc.text_pages} صفحة بدون نص قابل للاستخراج")
    return ImportedContent(title=pc.title or Path(name).stem, source_type="pdf",
                           blocks=pc.blocks, courses=pc.courses, version=pc.version,
                           academic_year=pc.academic_year, page_count=pc.page_count,
                           warnings=warnings)


def _import_text(text: str, name: str) -> ImportedContent:
    text = clean_text(text)
    blocks: list[Block] = []
    section = ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        first = para.splitlines()[0]
        if len(para.splitlines()) == 1 and len(first) < 80 and not first.endswith((".", "؟", ":")):
            section = first
            blocks.append(Block(first, "heading", 0, section))
        else:
            blocks.append(Block(para, "para", 0, section))
    return ImportedContent(title=Path(name).stem, source_type="text", blocks=blocks,
                           academic_year=find_academic_year(text[:3000]))


def _csv_rows(text: str) -> list[list[str]]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _import_rows(rows: list[list], name: str, sheet: str) -> ImportedContent:
    rows = [[clean_text("" if c is None else str(c)) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ImportError_("لا توجد بيانات في الجدول")
    headers, body = rows[0], rows[1:]
    mapping = map_columns(headers)
    blocks: list[Block] = []
    courses: list[dict] = []
    for i, row in enumerate(body, start=2):
        location = f"{sheet} — صف {i}"
        if "code" in mapping:
            values = {f: (row[j] if j < len(row) else "") for f, j in mapping.items()}
            course = row_to_course(values)
            if course:
                course["category"] = course["category"] or sheet
                courses.append(course)
                blocks.append(Block(course_passage(course), "table", 0, location))
                continue
        pairs = [f"{h}: {v}" if h else v for h, v in zip(headers, row) if v]
        if pairs:
            blocks.append(Block(" | ".join(pairs), "table", 0, location))
    warnings = []
    if "code" in mapping and not courses:
        warnings.append("تم العثور على عمود لرقم المساق لكن دون أرقام صالحة")
    return ImportedContent(title=Path(name).stem, source_type="dataset", blocks=blocks,
                           courses=courses, warnings=warnings)


def _import_xlsx(data: bytes, name: str) -> ImportedContent:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ImportError_(f"تعذّر فتح ملف Excel: {e}") from e
    merged = ImportedContent(title=Path(name).stem, source_type="dataset", blocks=[])
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if not any(any(c not in (None, "") for c in r) for r in rows):
            continue
        part = _import_rows(rows, name, sheet=ws.title)
        merged.blocks += part.blocks
        merged.courses += part.courses
        merged.warnings += part.warnings
    if not merged.blocks:
        raise ImportError_("ملف Excel لا يحتوي على بيانات")
    return merged


def _import_json(text: str, name: str) -> ImportedContent:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportError_(f"ملف JSON غير صالح: {e}") from e
    records = None
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        records = data
    elif isinstance(data, dict):
        for key in ("courses", "data", "items", "records", "المساقات"):
            if isinstance(data.get(key), list) and all(isinstance(x, dict) for x in data[key]):
                records = data[key]
                break
    if records:
        headers = list(dict.fromkeys(k for r in records for k in r))
        rows = [headers] + [[_json_cell(r.get(h)) for h in headers] for r in records]
        return _import_rows(rows, name, sheet="JSON")
    flat = "\n".join(_flatten(data))
    return _import_text(flat, name)


def _json_cell(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else str(v)


def _flatten(obj, prefix: str = "") -> list[str]:
    if isinstance(obj, dict):
        out = []
        for k, v in obj.items():
            out += _flatten(v, f"{prefix}{k}: " if not prefix else f"{prefix}{k} / ")
        return out
    if isinstance(obj, list):
        out = []
        for x in obj:
            out += _flatten(x, prefix)
        return out
    return [f"{prefix}{obj}"]
