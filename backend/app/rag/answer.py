"""Grounded Arabic answers with real citations.

Order of operations for every question:
  1. route to exactly one university (or ask / refuse)
  2. retrieve only that university's passages
  3. if the evidence gate fails -> say the official sources do not contain the answer
  4. otherwise generate (LLM) or quote (extractive) using ONLY those passages
  5. citations are built from stored metadata, never from model output
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .. import db
from .llm import LLMError, get_llm
from .evidence import content_terms
from .retriever import Passage, retrieve
from .router import route, strip_university_names

log = logging.getLogger("uniguide.answer")

NO_ANSWER_TOKEN = "NO_ANSWER"
CONFLICT_PREFIX = "⚠️ تعارض بين المصادر"

SYSTEM_PROMPT = f"""أنت "UniGuide AI"، مرشد أكاديمي لطلبة هندسة الحاسوب في جامعة القدس.
تفهم أسئلة الطلبة باللهجة الفلسطينية العامية، وتجيب دائماً بالعربية الفصحى المبسطة.

قواعد صارمة لا يجوز مخالفتها:
1. أجب فقط من المقاطع الرسمية المرقّمة المرفقة. لا تستخدم معلوماتك العامة أو ذاكرتك أبداً.
2. بعد كل معلومة ضع رقم المقطع الذي أخذتها منه بين قوسين مربعين مثل [1] أو [2].
3. قد تكون المقاطع بالإنجليزية: ترجم المعنى إلى العربية بدقة، واترك أرقام المساقات وأسماءها
   الإنجليزية والأرقام كما هي تماماً.
4. إذا لم تحتوِ المقاطع على إجابة السؤال، اكتب فقط: {NO_ANSWER_TOKEN}
5. إذا كانت الإجابة جزئية، أجب بما هو موجود فقط وقل صراحةً ما لم تذكره المصادر.
6. إذا تعارضت المقاطع (مثل رقمين مختلفين لنفس الأمر)، ابدأ بعبارة "{CONFLICT_PREFIX}:" واعرض الروايتين.
7. لا تخترع أرقاماً أو شروطاً أو مساقات غير موجودة في المقاطع، ولا تضف نصائح عامة.
8. كن موجزاً ومنظماً (نقاط قصيرة عند الحاجة).
9. اكتب بالعربية فقط. لا تكتب أي كلمة إنجليزية إلا أسماء المساقات ورموزها كما وردت حرفياً.
10. اقرأ صفوف الجداول بدقة: في صف مثل
    "Requirements: University Requirements | Compulsory: 18 | Elective: 6 | Free: - | Total: 24"
    الإجباري 18 والاختياري 6 ولا يوجد حر والمجموع 24. لا تخلط بين المجموع وأي عمود آخر.
11. لا تكتب كلمة "المقطع"؛ يكفي رقم المصدر بين قوسين مربعين.

مصطلحات الترجمة الصحيحة:
Compulsory = إجبارية، Elective = اختيارية، Free = حرة، credit hours = ساعات معتمدة،
Prerequisite = متطلب سابق، Corequisite = متطلب متزامن، University Requirements = متطلبات الجامعة،
Faculty Requirements = متطلبات الكلية، Program Requirements = متطلبات التخصص،
Graduation Project = مشروع التخرج، Internship = التدريب الميداني، Fall = الفصل الأول،
Spring = الفصل الثاني، Grand Total = المجموع الكلي."""

MSG_HELLO = ("أهلاً! أنا مرشد طلبة هندسة الحاسوب في جامعة القدس. أجيب من الخطة الدراسية الرسمية "
             "عن: مساقات كل سنة وفصل، المتطلبات السابقة لأي مادة، وصف المواد، المواد الاختيارية "
             "والإجبارية، الساعات المطلوبة للتخرج، التدريب الميداني ومشروع التخرج، وشروط القبول. "
             "مثال: «شو المتطلب السابق لمادة هياكل البيانات؟»")
_SMALL_TALK = re.compile(
    r"^\s*(مرحبا|مرحبتين|اهلا|أهلا|هلا|هاي|السلام عليكم|سلام|صباح الخير|مساء الخير|شكرا|شكراً|"
    r"يعطيك العافية|hi|hello|hey|thanks|thank you)(\s+\S+)?\s*[!؟?.،]*\s*$", re.IGNORECASE)

MSG_ASK = ("قبل أن أجيب، عن أي جامعة تسأل؟ اختر جامعتك من القائمة أعلاه حتى أبحث في وثائقها "
           "الرسمية فقط، لأن الأنظمة الأكاديمية تختلف من جامعة لأخرى.")
MSG_NO_ANSWER = ("لم أجد إجابة لهذا السؤال في المصادر الرسمية المفهرسة لـ{uni}. "
                 "لا أستطيع الإجابة من خارج الوثائق المعتمدة. يُنصح بمراجعة دائرة القبول والتسجيل "
                 "أو المرشد الأكاديمي، أو إضافة الوثيقة الرسمية ذات العلاقة إلى مصادر المعلومات.")
MSG_EMPTY = ("لا توجد حالياً أي وثائق مفهرسة لـ{uni}. يرجى من المسؤول إضافة المصادر الرسمية "
             "من صفحة «مصادر المعلومات الأكاديمية».")


def _source_label(m: dict) -> str:
    parts = [f"{m.get('university_name', '')} – {m.get('title', '')}"]
    if m.get("page"):
        parts.append(f"صفحة {m['page']}")
    if m.get("article"):
        parts.append(f"المادة {m['article']}")
    if m.get("section") and m.get("section") != m.get("title"):
        parts.append(f"قسم: {m['section'][:80]}")
    date = m.get("version") or m.get("retrieved_at")
    if date:
        parts.append(f"التاريخ/الإصدار: {date}")
    return " | ".join(parts)


def build_prompt(question: str, university: dict, passages: list[Passage]) -> str:
    blocks = []
    for i, p in enumerate(passages, start=1):
        blocks.append(f"[{i}] ({_source_label(p.meta)})\n{p.text}")
    return (f"الجامعة: {university['name_ar']}\n\nالمقاطع الرسمية:\n\n" + "\n\n".join(blocks)
            + f"\n\nسؤال الطالب: {question}\n\nالإجابة:")


def citation_for(n: int, p: Passage) -> dict[str, Any]:
    m = p.meta
    doc_id = int(m.get("doc_id") or 0)
    page = int(m.get("page") or 0)
    url = m.get("url") or ""
    if url:
        link = f"{url}#page={page}" if (m.get("source_type") == "pdf" and page) else url
    elif doc_id:
        link = f"/api/documents/{doc_id}/file" + (f"#page={page}" if page else "")
    else:
        link = ""
    return {
        "n": n,
        "university": m.get("university_name", ""),
        "university_id": m.get("university_id", ""),
        "collection": m.get("collection", p.collection),
        "source_type": m.get("source_type", ""),
        "title": m.get("title", ""),
        "url": url,
        "link": link,
        "file_ref": m.get("file_ref", ""),
        "page": page or None,
        "section": m.get("section", ""),
        "article": m.get("article", ""),
        "academic_year": m.get("academic_year", ""),
        "version": m.get("version", ""),
        "retrieved_at": m.get("retrieved_at", ""),
        "passage": p.text,
        "kind": p.kind,
        "doc_id": doc_id,
    }


_CITE = re.compile(r"\[(\d{1,2})\]")


def _dates_differ(passages: list[Passage]) -> bool:
    docs = {}
    for p in passages:
        docs[p.meta.get("doc_id")] = p.meta.get("version") or p.meta.get("retrieved_at")
    return len(docs) > 1 and len(set(docs.values())) > 1


def answer(question: str, university_id: str | None = None) -> dict[str, Any]:
    question = (question or "").strip()
    if not question:
        return {"status": "error", "answer": "الرجاء كتابة سؤال.", "citations": []}

    if _SMALL_TALK.match(question):
        return {"status": "greeting", "answer": MSG_HELLO, "citations": []}

    r = route(question, university_id)
    if r.status == "ask":
        return {"status": "needs_university", "answer": MSG_ASK, "citations": [],
                "universities": [{"id": u["id"], "name": u["name_ar"]}
                                 for u in db.list_universities()]}
    if r.status == "multiple":
        return {"status": "needs_university", "citations": [],
                "answer": "سؤالك يذكر أكثر من جامعة (" + "، ".join(r.mentioned) + "). "
                          "لا أدمج أنظمة جامعات مختلفة في إجابة واحدة؛ اسأل عن كل جامعة على حدة."}
    if r.status == "unknown_university":
        names = "، ".join(r.mentioned)
        return {"status": "no_answer", "citations": [], "university": None,
                "answer": f"لا تتوفر لديّ وثائق أكاديمية معتمدة لـ«{names}». أعتمد فقط على الوثائق "
                          "الرسمية المفهرسة لكل جامعة، ولن أستخدم أنظمة جامعة أخرى للإجابة عنها."}

    uni = r.university
    assert uni is not None
    uni_info = {"id": uni["id"], "name": uni["name_ar"]}
    ret = retrieve(strip_university_names(question, uni), uni)
    debug = {"best_similarity": round(ret.best_similarity, 4), "expanded_query": ret.expanded_query,
             "retrieved": len(ret.passages),
             "evidence": "covered" if ret.has_evidence else
             ("missing: " + " ".join(ret.missing) if ret.missing else
              "no specific subject in question" if not content_terms(question) else
              "low similarity")}
    if not ret.passages and ret.best_similarity == 0:
        return {"status": "no_answer", "answer": MSG_EMPTY.format(uni=uni["name_ar"]),
                "citations": [], "university": uni_info, "debug": debug}
    if not ret.has_evidence:
        return {"status": "no_answer", "answer": MSG_NO_ANSWER.format(uni=uni["name_ar"]),
                "citations": [], "university": uni_info, "debug": debug}

    covered = [p for p in ret.passages if p.extra.get("covered")]
    passages = covered[:LLM_PASSAGES] if covered else ret.passages[:LLM_PASSAGES]
    llm = get_llm()
    if llm is None:
        return _extractive(passages, uni_info, debug)

    prompt = build_prompt(question, uni, passages)
    text, problems = "", []
    for attempt in range(2):
        try:
            text = llm.generate(SYSTEM_PROMPT, prompt if attempt == 0 else prompt + (
                "\n\nالمحاولة السابقة رُفضت للأسباب التالية، أعد كتابة الإجابة دون هذه الأخطاء: "
                + "؛ ".join(problems)))
        except LLMError as e:
            log.warning("LLM failed, falling back to extractive mode: %s", e)
            out = _extractive(passages, uni_info, debug)
            out["notice"] = "تعذّر الاتصال بالنموذج اللغوي؛ تُعرض النصوص الرسمية كما هي."
            return out
        if NO_ANSWER_TOKEN in text and len(text) < 40 + len(NO_ANSWER_TOKEN):
            break
        problems = _generation_problems(text, passages)
        if not problems:
            break
    else:
        log.warning("generation rejected twice (%s); quoting the sources instead", problems)
        out = _extractive(passages, uni_info, debug)
        out["debug"] = {**debug, "rejected_generation": problems}
        return out

    if NO_ANSWER_TOKEN in text and len(text) < 40 + len(NO_ANSWER_TOKEN):
        return {"status": "no_answer", "answer": MSG_NO_ANSWER.format(uni=uni["name_ar"]),
                "citations": [], "university": uni_info, "mode": llm.name, "debug": debug}
    text = text.replace(NO_ANSWER_TOKEN, "").strip()

    used = [int(n) for n in _CITE.findall(text) if 1 <= int(n) <= len(passages)]
    used = list(dict.fromkeys(used)) or [1]
    cited = [passages[n - 1] for n in used]

    # Grounding check against the passages the model actually cited.
    invented = _ungrounded_numbers(text, cited)
    if invented:
        log.warning("model stated numbers not in its cited sources %s; quoting instead", invented)
        out = _extractive(passages, uni_info, debug)
        out["debug"] = {**debug, "rejected_generation": f"numbers not in sources: {invented}"}
        return out

    citations = [citation_for(n, passages[n - 1]) for n in used]
    text = _inline_citations(text, passages)
    status = "conflict" if CONFLICT_PREFIX in text else "answered"
    return {"status": status, "answer": text + _source_line(citations), "citations": citations,
            "university": uni_info, "mode": llm.name, "model": llm.label,
            "multi_source": _dates_differ(cited), "debug": debug}


LLM_PASSAGES = 5
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")


def _generation_problems(text: str, passages: list[Passage]) -> list[str]:
    """Reasons to reject a generated answer: invented figures or stray English words."""
    problems = []
    invented = _ungrounded_numbers(text, passages)
    if invented:
        problems.append("أرقام غير موجودة في المقاطع: " + "، ".join(invented))
    source = " ".join(p.text for p in passages).lower()
    stray = sorted({w for w in _LATIN_WORD.findall(_CITE.sub(" ", text))
                    if w.lower() not in source})
    if stray:
        problems.append("كلمات إنجليزية ليست في المقاطع: " + "، ".join(stray))
    return problems
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_LIST_MARKER = re.compile(r"(?m)^\s*[-•*]?\s*\d{1,2}\s*[.)\-–]\s")


def _ungrounded_numbers(text: str, cited: list[Passage]) -> list[str]:
    """Numbers in the answer (course codes, hours, averages…) that no cited passage contains."""
    body = _LIST_MARKER.sub(" ", _CITE.sub(" ", text)).translate(_AR_DIGITS)
    source = " ".join(p.text for p in cited).translate(_AR_DIGITS)
    have = set(_NUMBER.findall(source))
    return sorted({n for n in _NUMBER.findall(body) if n not in have})


_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _source_line(citations: list[dict[str, Any]]) -> str:
    """«المصدر: …، صفحة N» built from stored metadata (never from model output)."""
    seen, parts = set(), []
    for c in citations:
        key = (c["doc_id"], c.get("page"))
        if key in seen:
            continue
        seen.add(key)
        parts.append(c["title"] + (f"، صفحة {c['page']}" if c.get("page") else ""))
    if not parts:
        return ""
    label = "المصدر" if len(parts) == 1 else "المصادر"
    return f"\n\n{label}: " + "؛ ".join(parts)


def _inline_citations(text: str, passages: list[Passage]) -> str:
    """Replace the model's «[n]» with the real page of passage n, e.g. «(ص 3)» (from metadata)."""
    def page_ref(m: re.Match) -> str:
        n = int(m.group(1))
        page = passages[n - 1].meta.get("page") if 1 <= n <= len(passages) else None
        return f" (ص {page})" if page else ""
    text = _CITE.sub(page_ref, text)
    text = re.sub(r"(\(ص \d+\))(\s*)+", r"", text)          # «(ص 3) (ص 3)» -> «(ص 3)»
    return re.sub(r"[ 	]+([.،,؛:!؟?])", r"", re.sub(r"[ 	]{2,}", " ", text)).strip()


def _strip_markers(text: str) -> str:
    """Answers are shown without source numbers: drop any «[n]» the model still wrote."""
    text = _CITE.sub("", text)
    return re.sub(r"[ \t]+([.،,؛:!؟?])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _extractive(passages: list[Passage], uni_info: dict, debug: dict) -> dict[str, Any]:
    """No LLM: answer with the best-matching official passage (plus the next one if it is short)."""
    top = passages[:3]
    records = [p for p in passages if p.kind != "chunk"]
    if records:
        # Structured records built from the document's own tables are complete answers.
        shown = records[:3]
        body = "\n\n".join(p.text for p in shown)
    else:
        shown = [top[0]]
        if len(top[0].text) < 300 and len(top) > 1:
            shown.append(top[1])
        body = "\n\n".join(p.text if len(p.text) <= 900 else p.text[:900] + "…" for p in shown)
    top = shown + [p for p in top if p not in shown][: max(0, 3 - len(shown))]
    body += _source_line([citation_for(i, p) for i, p in enumerate(shown, start=1)])
    return {"status": "answered", "answer": body, "mode": "extractive",
            "citations": [citation_for(i, p) for i, p in enumerate(top, start=1)],
            "university": uni_info, "multi_source": _dates_differ(top), "debug": debug}
