"""Answerability: does a retrieved passage actually cover what the question asks about?

Embedding similarity only says a passage is *about the same area* (every question about a
study plan looks similar to the study plan). It cannot tell "what is the price of a credit
hour?" from "how many credit hours do I need?". So an answer is only allowed when one passage
accounts for every meaningful word of the question:

  * the word itself appears in the passage (Arabic or English, light normalisation), or
  * the word is part of an Arabic academic term whose English equivalent appears in the
    passage (English-language study plans, see ACADEMIC_GLOSSARY), or
  * the word carries no content of its own (question words, dialect fillers, words that
    describe every passage of an academic plan such as «مادة» or «هندسة الحاسوب»).

A single unaccounted word («سعر», «رئيس», «منح», «تعلم الآلة») means the sources do not
contain the answer, and the assistant refuses instead of quoting something merely similar.
"""
from __future__ import annotations

import re

from ..text.arabic import ACADEMIC_GLOSSARY, normalize_for_search, tokenize

# Words that never carry the subject of a question (normalised, definite article stripped).
_FUNCTION_WORDS = {
    # interrogatives / particles (MSA + Palestinian)
    "شو", "ما", "ماذا", "كم", "متي", "امتي", "وين", "اين", "مين", "من", "هل", "كيف", "ليش",
    "لماذا", "اي", "ايش", "اذا", "لو", "يعني", "بس", "كمان", "ولا", "او", "و", "في", "على",
    "علي", "عن", "الي", "مع", "بين", "قبل", "بعد", "بعدها", "عند", "عندي", "هاي", "هاد", "هذا",
    "هذه", "ذلك", "تلك", "كل", "اكثر", "اقل", "فيه", "فيها", "منها", "منه",
    # pronouns and helpers
    "انا", "انت", "احنا", "نحن", "هو", "هي", "هم", "اللي", "لي", "الذي", "التي", "الذين",
    "لازم", "يجب", "بدي", "بدنا", "بدك", "بقدر", "بنقدر", "استطيع", "يمكن", "ممكن", "يمكنني",
    "عشان", "لكي", "حتي", "منشان", "ضروري", "مطلوب", "مطلوبه", "المطلوب",
    # generic student verbs (the subject is elsewhere in the question)
    "اخلص", "خلص", "مخلص", "اخذ", "اخد", "ناخذ", "بنوخذ", "بناخذ", "بوخذ", "باخذ",
    "اكون", "يكون", "بكون", "صار", "بصير", "اسجل", "سجل", "نسجل", "تسجيل", "انزل", "نزل",
    "تنزيل", "اختار", "نختار", "اعرف", "رسبت", "سقطت", "نجحت", "رسب", "درست", "ادرس",
    "تعطيني", "اعطيني", "قلي", "خبرني", "احكيلي", "بتكون", "يتم",
    # quantity / scope words and "what does it cover" verbs (Palestinian + MSA)
    "قديش", "اديش", "قداش", "كلها", "كله", "كلهم", "جميع", "كامل", "كامله", "مجموع", "اجمالي",
    "بتعلمنا", "بتعلم", "بنتعلم", "نتعلم", "تعلمنا", "بندرس", "ندرس", "بتدرسنا", "بتشمل", "تشمل",
    "يشمل", "بتحكي", "محتوي", "تحتوي", "بتحتوي", "عباره", "يعني", "هي", "هو", "شغله", "شغلات",
    "دراسه", "الدراسه", "بنوخذها", "بناخذها", "اخذها", "اخلصها", "انزلها", "اسجلها",
    "مرحبا", "اهلا", "هلا", "سلام", "عليكم", "لو", "سمحت", "شكرا", "please", "tell", "about",
    # describe every passage of a programme's study plan
    "ماده", "مواد", "مساق", "مساقات", "ساعه", "ساعات", "معتمده", "هندسه",
    "حاسوب", "كمبيوتر", "تخصص", "برنامج", "خطه", "دراسيه", "دراسي", "جامعه", "قدس",
    "كليه", "بكالوريوس", "عدد", "نوع", "شي", "اشي", "اشياء",
    # English function words
    "what", "which", "who", "when", "where", "how", "many", "much", "is", "are", "the", "of",
    "a", "an", "for", "to", "in", "on", "do", "does", "i", "my", "me", "need", "can", "course",
    "courses", "credit", "credits", "hours", "computer", "engineering", "program", "plan",
}
# English words too common in an academic plan to prove anything on their own.
_GENERIC_EN = {"requirements", "requirement", "course", "courses", "total", "program", "study",
               "distribution", "topics", "computer", "engineering", "systems", "introduction",
               "fundamentals", "lab", "laboratory", "credit", "hours", "credits", "for"}

_PROCLITICS = ("وبال", "وال", "بال", "فال", "كال", "لل", "و", "ب", "ل", "ف", "ك")
_SUFFIXES = ("هم", "ها", "كم", "نا", "ه", "ي", "ك")
_WORD = re.compile(r"[a-z0-9]+|[ء-ي]+")

_GLOSSARY = [(normalize_for_search(k), v.lower()) for k, v in ACADEMIC_GLOSSARY.items()]


def _variants(tok: str, min_stem: int = 3) -> set[str]:
    """The token plus forms without a leading particle / trailing pronoun («لطلاب», «اسجلها»)."""
    out = {tok}
    for p in _PROCLITICS:
        if tok.startswith(p) and len(tok) - len(p) >= min_stem:
            out.add(tok[len(p):])
    for v in list(out):
        for s in _SUFFIXES:
            if v.endswith(s) and len(v) - len(s) >= 3:
                out.add(v[: -len(s)])
    return out


def content_terms(question: str) -> list[str]:
    """The question words that carry its subject."""
    toks = []
    for t in tokenize(question):
        # short function words can carry a particle too: «بأي» = ب + أي
        if not any(v in _FUNCTION_WORDS for v in _variants(t, min_stem=2)):
            toks.append(t)
    return list(dict.fromkeys(toks))


def missing_terms(question: str, passage: str) -> list[str]:
    """Meaningful question words that this passage does not account for (empty = covered)."""
    content = content_terms(question)
    if not content:
        return []
    p_norm = normalize_for_search(passage)
    p_tokens = set(tokenize(passage))
    p_words = set(_WORD.findall(p_norm))
    qn = normalize_for_search(question)

    # Arabic academic terms whose English equivalent is present in this passage.
    bridged: set[str] = set()
    for k, v in _GLOSSARY:
        if k not in qn:
            continue
        words = {w for w in _WORD.findall(v) if len(w) >= 3}
        # specific English words prove the term; a purely generic term («مختبر» -> lab) may
        # match its generic word
        alts = {w for w in words if len(w) >= 4 and w not in _GENERIC_EN} or words
        if v in p_norm or (alts & p_words):
            bridged |= set(tokenize(k))
    missing = []
    for t in content:
        forms = _variants(t)
        if forms & p_tokens or forms & bridged or forms & p_words:
            continue
        missing.append(t)
    return missing
