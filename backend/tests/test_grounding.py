"""Generation guard rails: numbers must come from cited passages; filler-only questions."""
from app.rag.answer import _source_line, _ungrounded_numbers
from app.rag.evidence import content_terms
from app.rag.retriever import Passage


def _p(text, page=3):
    return Passage(id="x", text=text, meta={"page": page}, collection="c")


def test_invented_numbers_are_detected():
    src = [_p("The degree is awarded upon completing a total of 166 credit hours.")]
    assert _ungrounded_numbers("يجب إكمال 166 ساعة معتمدة [1].", src) == []
    assert _ungrounded_numbers("يجب إكمال 170 ساعة معتمدة [1].", src) == ["170"]
    # Arabic-Indic digits are the same numbers; list markers are not facts
    assert _ungrounded_numbers("١٦٦ ساعة", src) == []
    assert _ungrounded_numbers("1. إكمال 166 ساعة\n2. مشروع التخرج", src) == []


def test_source_line_comes_from_metadata():
    line = _source_line([{"doc_id": 7, "page": 3, "title": "الخطة الدراسية"},
                         {"doc_id": 7, "page": 3, "title": "الخطة الدراسية"}])
    assert line.strip() == "المصدر: الخطة الدراسية، صفحة 3"


def test_filler_only_questions_name_no_subject():
    assert content_terms("كم ساعة بقدر أسجل؟") == []
    assert content_terms("شو بصير إذا رسبت بمادة؟") == []
    assert content_terms("شو شروط الانسحاب من مادة؟") == ["شروط", "انسحاب"]
