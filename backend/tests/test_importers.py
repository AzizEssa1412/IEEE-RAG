"""Dataset importers (PDF/TXT/CSV/JSON/XLSX) and structured course recognition."""
import json
from pathlib import Path

import fitz
import pytest
from openpyxl import Workbook

from app.ingest.courses import map_columns, parse_requisites
from app.ingest.importers import ImportError_, import_file

REAL_PLAN = Path(__file__).resolve().parents[2] / "data" / "uploads" / \
    "AcademicPlan-AQU-Eng-CE2010.pdf"


def test_csv_courses_and_prerequisite_relations(fixtures_dir):
    c = import_file(fixtures_dir / "SYNTHETIC_courses_for_testing.csv")
    assert c.source_type == "dataset" and len(c.courses) == 5
    algo = next(x for x in c.courses if x["code"] == "9990201")
    kinds = {(r["kind"], r["code"]) for r in algo["requisites"]}
    assert kinds == {("prerequisite", "9990102"), ("corequisite", "9990202")}
    assert algo["credits"] == "3" and algo["department"] == "قسم اصطناعي"
    assert algo["semester"] == "السنة الثانية - الفصل الأول"
    # "See Note 1" with no note text available stays an explicit, un-invented condition
    proj = next(x for x in c.courses if x["code"] == "9990301")
    assert proj["requisites"] == [{"kind": "condition", "code": "", "raw": "See Note 1"}]


def test_txt_import_keeps_articles(fixtures_dir):
    c = import_file(fixtures_dir / "SYNTHETIC_regulations_for_testing.txt")
    assert c.source_type == "text"
    assert any("18 ساعة معتمدة" in b.text for b in c.blocks)


def test_json_and_xlsx_course_records(tmp_path):
    rows = [{"course_code": "9990101", "course_name": "Synthetic Intro", "credit_hours": 3,
             "prerequisites": []},
            {"course_code": "9990102", "course_name": "Synthetic DS", "credit_hours": 3,
             "prerequisites": ["9990101"]}]
    jp = tmp_path / "synthetic.json"
    jp.write_text(json.dumps({"courses": rows}), encoding="utf-8")
    jc = import_file(jp)
    assert [x["code"] for x in jc.courses] == ["9990101", "9990102"]
    assert jc.courses[1]["requisites"][0]["code"] == "9990101"

    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["Course No.", "Course Name", "Credits", "Prerequisites"])
    ws.append(["9990201", "Synthetic Algorithms", 3, "P-9990102"])
    ws.append([None, None, None, None])
    xp = tmp_path / "synthetic.xlsx"
    wb.save(xp)
    xc = import_file(xp)
    assert xc.courses[0]["code"] == "9990201" and xc.courses[0]["category"] == "Plan"


def test_column_mapping_arabic_and_english():
    m = map_columns(["رقم المساق", "اسم المساق", "الساعات المعتمدة", "المتطلب السابق"])
    assert m == {"code": 0, "title": 1, "credits": 2, "prerequisites": 3}
    m = map_columns(["Course\nNo.", "Course Name", "Th", "Lab", "Prerequisites\n/Corequisites"])
    assert m["code"] == 0 and m["credits_theory"] == 2 and m["prerequisites"] == 4


def test_parse_requisites_with_notes():
    notes = {"1": "P-0702453+ Student must have successfully completed a minimum of 110 Credit Hours."}
    reqs = parse_requisites("See Note 1", notes)
    assert {"kind": "prerequisite", "code": "0702453", "raw": notes["1"]} in reqs
    assert any(r["kind"] == "condition" and "110" in r["raw"] for r in reqs)
    assert parse_requisites("---") == []
    assert parse_requisites("3rd year +")[0]["kind"] == "condition"


def test_pdf_page_numbers_and_scanned_pdf_rejected(tmp_path):
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Synthetic regulation page {i + 1}. Article {i + 1} text.")
    p = tmp_path / "synthetic.pdf"
    doc.save(p)
    c = import_file(p)
    assert c.page_count == 3
    assert {b.page for b in c.blocks} == {1, 2, 3}

    blank = fitz.open()
    blank.new_page()
    bp = tmp_path / "scanned.pdf"
    blank.save(bp)
    with pytest.raises(ImportError_, match="OCR"):
        import_file(bp)


def test_unsupported_and_empty_files(tmp_path):
    bad = tmp_path / "a.docx"
    bad.write_bytes(b"x")
    with pytest.raises(ImportError_):
        import_file(bad)
    empty = tmp_path / "e.csv"
    empty.write_bytes(b"")
    with pytest.raises(ImportError_):
        import_file(empty)


@pytest.mark.skipif(not REAL_PLAN.exists(), reason="real study plan PDF not present")
def test_real_alquds_ce_plan_courses():
    c = import_file(REAL_PLAN)
    codes = {x["code"]: x for x in c.courses}
    assert len(c.courses) >= 95
    ds = codes["0702211"]
    assert ds["title"] == "Data Structures" and ds["credits"] == "3" and ds["page"] == 6
    assert ds["semester"].startswith("Second Year")
    sp = codes["0702492"]
    assert any(r["kind"] == "condition" and "110" in r["raw"] for r in sp["requisites"])
    # The document's own inconsistencies are preserved, not "fixed"
    assert any(b.page == 3 and "166" in b.text for b in c.blocks)
    assert any(b.page == 3 and "168" in b.text for b in c.blocks)
