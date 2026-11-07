from app.text.arabic import (clean_text, dialect_to_msa, expand_query, find_academic_year,
                             find_article_number, fix_reversed_arabic_line, normalize_for_search,
                             tokenize)
from app.text.chunking import Block, chunk_blocks


def test_clean_text_folds_pdf_presentation_forms_and_keeps_lines():
    # "ﺍﻟﻄﺎﻟﺐ" written with Arabic presentation forms (as many PDFs emit it)
    assert clean_text("ﺍﻟﻄﺎﻟﺐ‏  ـــ  \n\n\n\nالجامعة") == "الطالب\n\nالجامعة"


def test_normalize_for_search_unifies_letter_variants_and_digits():
    assert normalize_for_search("إدارة الأُسرة ١٢") == "اداره الاسره 12"


def test_tokenize_strips_definite_article():
    assert "تسجيل" in tokenize("التسجيل والانسحاب")


def test_dialect_expansion_maps_palestinian_terms():
    extra = dialect_to_msa("كم ساعة بقدر أنزل بالفصل؟")
    assert "أستطيع" in extra and "تسجيل" in extra and "الفصل الدراسي" in extra
    assert expand_query("شو شروط التخرج؟").startswith("شو شروط التخرج؟")


def test_article_and_academic_year_detection():
    assert find_article_number("المادة (12): الانسحاب") == "12"
    assert find_article_number("المادة الثالثة: التسجيل") == "3"
    assert find_article_number("بدون مادة مرقمة") == ""
    assert find_academic_year("التخصصات المطروحة للفصل الأول 2027/2026") == "2026/2027"
    assert find_academic_year("سنة 2010") == ""


def test_reversed_arabic_line_is_repaired():
    original = "يجب على الطالب مراجعة المرشد"
    assert fix_reversed_arabic_line(original[::-1]) == original
    assert fix_reversed_arabic_line(original) == original
    # Real cell from the Al-Quds admission bulletin (place names only, no function words)
    assert fix_reversed_arabic_line("سيدوبأ: هللا مار ،ارود،سيدوبأ").startswith("أبوديس")
    # Normal text ending in «...لا» must not be flipped
    assert fix_reversed_arabic_line("مثلا يمكن للطالب") == "مثلا يمكن للطالب"


def test_chunks_respect_page_section_and_never_split_table_rows():
    row = "رقم المساق: 0702211 | اسم المساق: هياكل البيانات | الساعات: 3"
    blocks = [Block("مقدمة", "heading", 1, "مقدمة"), Block("نص " * 120, "para", 1, "مقدمة")]
    blocks += [Block(row, "table", 2, "الجدول") for _ in range(30)]
    chunks = chunk_blocks(blocks, size=400, overlap=50)
    assert {c.page for c in chunks} == {1, 2}
    for c in chunks:
        if c.page == 2:
            assert all(line == row for line in c.text.splitlines())
            assert c.section == "الجدول"


def test_chunks_record_article_numbers():
    blocks = [Block("المادة (5): الحد الأعلى للتسجيل", "heading", 0, "س"),
              Block("يجوز للطالب تسجيل 18 ساعة. " * 20, "para", 0, "س"),
              Block("المادة (6): الانسحاب", "heading", 0, "س"),
              Block("يحق للطالب الانسحاب. " * 20, "para", 0, "س")]
    chunks = chunk_blocks(blocks, size=300, overlap=0)
    arts = {c.article for c in chunks}
    assert arts == {"5", "6"}
    assert any("الانسحاب" in c.text and c.article == "6" for c in chunks)
