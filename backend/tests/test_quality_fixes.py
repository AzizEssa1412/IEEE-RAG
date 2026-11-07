"""Regression tests for issues found while testing against the real Al-Quds index."""
from app.ingest.calendar import calendar_blocks, parse_rows
from app.ingest.pdf import _running_lines, _line_key, fix_reversed_word_order
from app.rag.router import route, strip_university_names


def test_definite_al_jamiaa_is_not_a_named_university():
    # «بالجامعة» = "at the university" (generic), previously misread as a university "وكم"
    r = route("شو اسم رئيس نادي الشطرنج بالجامعة وكم سعر الفلافل؟", "alquds")
    assert r.status == "ok" and r.university["id"] == "alquds"
    assert route("شو شروط التخرج بجامعة بيرزيت؟", "alquds").mentioned == ["جامعة بيرزيت"]


def test_university_name_is_stripped_before_retrieval(synthetic_university):
    from app import db

    uni = db.get_university("alquds")
    assert strip_university_names("شو الحد الأدنى لمعدل هندسة الحاسوب بجامعة القدس؟", uni) == \
        "شو الحد الأدنى لمعدل هندسة الحاسوب ؟"


def test_reversed_word_order_lines_are_repaired():
    visual = ".المعادلة الملف طريق عن بقبوله"
    assert fix_reversed_word_order(visual) == "بقبوله عن طريق الملف المعادلة."
    normal = "يتم إعلامه بقبوله عن طريق ملف المعادلة."
    assert fix_reversed_word_order(normal) == normal


def test_running_headers_and_footers_detected():
    pages = [[f"دليل الحصول على اللقب الأول {i} صفحة", f"محتوى الصفحة رقم {i} مختلف تماماً عن غيره {'x' * i}",
              "WWW.ALQUDS.EDU"] for i in range(1, 9)]
    rep = _running_lines(pages)
    assert _line_key("WWW.ALQUDS.EDU") in rep
    assert _line_key("دليل الحصول على اللقب الأول 3 صفحة") in rep
    assert _line_key(pages[0][1]) not in rep


def test_years_are_not_article_numbers_and_generic_anchors_are_not_titles():
    from app.ingest.pipeline import is_descriptive_anchor
    from app.text.arabic import find_article_number

    assert find_article_number("وفقاً للمادة 2003 من القانون") == ""
    assert find_article_number("المادة (12) الانسحاب") == "12"
    assert not is_descriptive_anchor("إضغط هنا لتحميل آلية التسجيل")
    assert not is_descriptive_anchor("إضغط هنا")
    assert is_descriptive_anchor("دليل البكالوريوس")


def test_calendar_feed_rows_become_self_describing_blocks():
    xml = ("<rows id='0'><row id='73'><cell><![CDATA[فترة الحذف والاضافة]]></cell>"
           "<cell><![CDATA[السبت    - 2026/09/19]]></cell><cell><![CDATA[الاربعاء - 2026/09/23]]>"
           "</cell><cell><![CDATA[1]]></cell></row></rows>")
    rows = parse_rows(xml)
    assert rows[0][:3] == ["فترة الحذف والاضافة", "السبت - 2026/09/19", "الاربعاء - 2026/09/23"]
    blocks = calendar_blocks(rows, "التقويم الأكاديمي 2026/2027 – الفصل الدراسي الأول")
    assert blocks[1].text == ("البند: فترة الحذف والاضافة | من: السبت - 2026/09/19 | "
                              "إلى: الاربعاء - 2026/09/23")
    assert blocks[1].kind == "table"
