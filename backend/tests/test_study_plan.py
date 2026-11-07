"""Study-plan extraction and Arabic access to English plans (synthetic data only)."""
from app.ingest.pdf import _attach_descriptions, _split_multi_course_rows, _table_to_rows
from app.text.arabic import academic_terms, expand_query
from app.text.chunking import Block


def test_two_courses_in_one_cell_become_two_rows():
    rows = [["9990111\n9990112", "Language I\nLanguage II", "3\n3"]]
    assert _split_multi_course_rows(rows) == [["9990111", "Language I", "3"],
                                              ["9990112", "Language II", "3"]]
    # a wrapped course name in a single-course row is left alone
    one = [["9990113", "Introduction to\nSomething", "3"]]
    assert _split_multi_course_rows(one) == one


class _FakeTable:
    def __init__(self, data):
        self.data = data

    def extract(self):
        return self.data


def test_semester_group_headers_survive_sub_headers():
    t = _FakeTable([["Fall Semester", None, None, "Spring Semester", None, None],
                    ["Course #", "Course Name", "Cr.", "Course #", "Course Name", "Cr."],
                    ["9990101", "Calculus I", "3", "9990102", "Calculus II", "3"]])
    names, rows, _ = _table_to_rows(t)
    assert names[0].startswith("Fall Semester") and names[3].startswith("Spring Semester")


def test_descriptions_attach_by_code_and_by_close_title():
    blocks = [Block("\uf0a8 9990201 Operating Systems (3 credits) Processes, threads and memory "
                    "management for modern systems.", "para", 17, "s"),
              Block("Engineering Mathmetics (3 credits)", "heading", 10, "s"),
              Block("Differential equations and linear algebra for engineering students.",
                    "para", 10, "s")]
    courses = [{"code": "9990201", "title": "Operating Systems"},
               {"code": "9990202", "title": "Engineering Mathematics"},
               {"code": "9990203", "title": "Unrelated Course Title"}]
    _attach_descriptions(blocks, courses)
    assert "threads" in courses[0]["description"] and courses[0]["description_page"] == 17
    assert "Differential equations" in courses[1]["description"]
    assert "description" not in courses[2]          # nothing is invented


def test_arabic_questions_reach_english_plan_terms():
    assert "Operating Systems" in academic_terms("شو بنوخذ في مادة نظم التشغيل؟")
    assert "Computer Engineering" in expand_query("شو مواد سنة أولى فصل أول هندسة حاسوب؟")
    assert "First Year" in expand_query("مواد السنة الأولى") and "Fall Semester" in \
        expand_query("مواد الفصل الأول")
    assert academic_terms("شو اسم رئيس نادي الشطرنج؟") == ""
