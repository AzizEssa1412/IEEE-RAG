"""Answerability: a passage must cover every meaningful word of the question."""
from app.rag.evidence import content_terms, missing_terms

PLAN = ("Structure and Contents: The B.Sc. degree is awarded upon completing a total of 120 "
        "credit hours. 9999101 Operating Systems (3 credits) Processes and threads. "
        "Prerequisite: 9999100. At least 100 field training hours.")


def test_fillers_and_dialect_are_not_content():
    assert content_terms("شو لازم آخذ عشان أتخرج؟") == ["اتخرج"]
    assert content_terms("مادة هندسة البرمجيات بأي سنة؟") == ["برمجيات", "سنه"]


def test_question_covered_through_english_equivalents():
    assert missing_terms("كم ساعة لازم أخلص عشان أتخرج؟", PLAN) == []
    assert missing_terms("كم ساعة تدريب ميداني لازم؟", PLAN) == []
    assert missing_terms("شو بنوخذ في مادة نظم التشغيل؟", PLAN) == []


def test_subject_absent_from_sources_is_reported():
    assert missing_terms("كم سعر الساعة المعتمدة؟", PLAN) == ["سعر"]
    assert missing_terms("مين رئيس قسم هندسة الحاسوب؟", PLAN) == ["رييس", "قسم"]
    assert "تعلم" in missing_terms("شو المتطلب السابق لمادة تعلم الآلة؟", PLAN)
    assert "teaches" in missing_terms("Who teaches Operating Systems?", PLAN)
