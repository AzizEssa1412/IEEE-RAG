"""Arabic text utilities: cleaning, normalisation, dialect handling, metadata extraction."""
from __future__ import annotations

import re
import unicodedata

_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_TATWEEL = "\u0640"
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ARABIC_CHAR = re.compile(r"[\u0600-\u06FF]")
_WS = re.compile(r"[ \t\u00A0\u200B\u200C\u200D\u200E\u200F\u202A-\u202E\u2066-\u2069]+")


def clean_text(text: str) -> str:
    """Display-safe cleanup that PRESERVES the original Arabic wording.

    - folds Arabic presentation forms (common in PDFs) back to normal letters (NFKC)
    - removes tatweel and bidi control characters, collapses whitespace
    - keeps line breaks (they carry table / list structure)
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(_TATWEEL, "")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def normalize_for_search(text: str) -> str:
    """Aggressive normalisation used ONLY for matching (never shown to users)."""
    text = clean_text(text).lower()
    text = _TASHKEEL.sub("", text)
    text = text.translate(_ARABIC_DIGITS)
    text = re.sub("[إأآٱ]", "ا", text)
    return text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")


_PREFIXES = ("وال", "بال", "كال", "فال", "لل", "ال")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_STOP = {"ما", "من", "في", "على", "عن", "الي", "ان", "او", "هل", "كيف", "هي", "هو", "مع",
         "شو", "بقدر", "اللي", "the", "of", "and", "to", "in", "is", "for", "a"}


def tokenize(text: str) -> list[str]:
    """Light Arabic tokenizer with definite-article stripping for lexical matching."""
    tokens: list[str] = []
    for tok in _TOKEN.findall(normalize_for_search(text)):
        if _ARABIC_CHAR.search(tok):
            for p in _PREFIXES:
                if tok.startswith(p) and len(tok) - len(p) >= 2:
                    tok = tok[len(p):]
                    break
        if tok in _STOP:
            continue
        if len(tok) >= 2 or tok.isdigit():
            tokens.append(tok)
    return tokens


def arabic_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _ARABIC_CHAR.match(c)) / len(letters)


# --- Repairing visually-ordered (reversed) Arabic that some PDFs produce ----------
_COMMON_WORDS = {"في", "من", "على", "الى", "إلى", "التي", "الذي", "عن", "مع", "او", "أو",
                 "الطالب", "الجامعة", "المادة", "الساعات", "الطلبة", "يجب", "هذه", "ان", "أن"}


def _score_words(line: str) -> int:
    return sum(1 for w in line.split() if w in _COMMON_WORDS)


def _orthography_score(line: str) -> int:
    """Positive when words look like normal Arabic: «ال» prefixes and «ة/ى» endings.

    No Arabic word starts with «ة» or «ى», so those (and «...لا» endings, i.e. a reversed
    «ال») are strong signs of character-reversed text.
    """
    score = 0
    for w in re.findall(r"[ء-ي]+", line):
        if w.startswith(("ال", "وال", "بال", "فال", "كال", "لل")) or w.endswith(("ة", "ى")):
            score += 1
        if w.startswith(("ة", "ى")) or (len(w) > 3 and w.endswith(("لا", "لاو", "لاب", "لل"))):
            score -= 1
    return score


def fix_reversed_arabic_line(line: str) -> str:
    """If an Arabic line looks character-reversed (visual order), reverse it back."""
    if arabic_ratio(line) < 0.5:
        return line
    rev = line[::-1]
    a = _score_words(line) * 2 + _orthography_score(line)
    b = _score_words(rev) * 2 + _orthography_score(rev)
    return rev if b > a else line


# --- Palestinian / Levantine dialect -> Modern Standard Arabic hints -------------
# Used to EXPAND the query; the student's original wording is always kept too.
DIALECT_MAP: dict[str, str] = {
    "شو": "ما", "ايش": "ما", "إيش": "ما", "شنو": "ما",
    "ليش": "لماذا", "وين": "أين", "منين": "من أين", "امتى": "متى", "إمتى": "متى",
    "قديش": "كم", "أديش": "كم", "اديش": "كم", "كام": "كم",
    "بقدر": "أستطيع يمكنني", "بنفع": "يمكن يجوز", "بصير": "يجوز يمكن",
    "بدي": "أريد", "بدنا": "نريد", "مش": "ليس لا", "هيك": "هكذا", "هلأ": "الآن", "هلق": "الآن",
    "اللي": "التي الذي", "إللي": "التي الذي", "زي": "مثل",
    "أنزل": "أسجل تسجيل", "انزل": "أسجل تسجيل", "نزلت": "سجلت", "تنزيل": "تسجيل",
    "أسحب": "سحب انسحاب", "اسحب": "سحب انسحاب", "سحبت": "سحب انسحاب",
    "رسبت": "رسوب رسب متطلب سابق", "سقطت": "رسوب رسب", "نجحت": "نجاح",
    "مادة": "مادة مساق", "بمادة": "مادة مساق", "مواد": "مواد مساقات",
    "ساعة": "ساعة معتمدة", "ساعات": "ساعات معتمدة",
    "بالفصل": "في الفصل الدراسي", "الفصل": "الفصل الدراسي",
    "بلاقي": "أجد", "ألاقي": "أجد", "الاقي": "أجد",
    "اتخرج": "التخرج", "أتخرج": "التخرج",
    "بجامعة": "في جامعة",
}
DIALECT_PHRASES: dict[str, str] = {
    "اللي بعدها": "المتطلب السابق للمساق اللاحق",
    "انزل مواد": "تسجيل المساقات",
    "كم ساعة": "عدد الساعات المعتمدة الحد الأعلى للتسجيل",
}

_PUNCT = re.compile(r"[؟?!.,،؛;:()\"'«»]")


def dialect_to_msa(query: str) -> str:
    """Return MSA expansion terms for a (possibly dialectal) question."""
    q = _PUNCT.sub(" ", query)
    extra: list[str] = [msa for phrase, msa in DIALECT_PHRASES.items() if phrase in q]
    for word in q.split():
        for cand in (word, word[1:] if word.startswith("و") and len(word) > 3 else None):
            if cand and cand in DIALECT_MAP:
                extra.append(DIALECT_MAP[cand])
                break
    return " ".join(dict.fromkeys(extra))


# --- Arabic -> English academic terms ---------------------------------------------
# Study plans are often published in English while students ask in Arabic. These are
# generic academic terms (no answers): they let an Arabic question reach English text.
ACADEMIC_GLOSSARY: dict[str, str] = {
    "هندسة الحاسوب": "Computer Engineering", "هندسة حاسوب": "Computer Engineering",
    "هندسة الكمبيوتر": "Computer Engineering", "هندسة كمبيوتر": "Computer Engineering",
    "مشروع التخرج": "Graduation Project", "مقدمة المشروع": "Introduction to Graduation Project",
    "التخرج": "graduation degree awarded total credit hours",
    "اتخرج": "graduation degree awarded total credit hours",
    "ساعة": "credit hours", "ساعات": "credit hours",
    "متطلب سابق": "prerequisite", "المتطلب السابق": "prerequisite", "متطلبات سابقة": "prerequisites",
    "متطلب متزامن": "corequisite",
    "اختياري": "elective courses", "اختيارية": "elective courses",
    "إجباري": "compulsory courses", "اجباري": "compulsory courses", "إجبارية": "compulsory courses",
    "اجبارية": "compulsory courses", "المطلوبة": "compulsory required courses",
    "متطلبات الجامعة": "University Requirements", "متطلب جامعة": "University Requirements",
    "متطلبات الكلية": "Faculty Requirements", "متطلب كلية": "Faculty Requirements",
    "متطلبات التخصص": "Program Requirements", "متطلبات البرنامج": "Program Requirements",
    "تدريب ميداني": "field training hours Internship", "تدريب عملي": "field training hours Internship",
    "التدريب": "Internship field training", "تدريب": "Internship field training",
    "سنة أولى": "First Year", "السنة الأولى": "First Year", "سنة اولى": "First Year",
    "سنة ثانية": "Second Year", "السنة الثانية": "Second Year",
    "سنة ثالثة": "Third Year", "السنة الثالثة": "Third Year",
    "سنة رابعة": "Fourth Year", "السنة الرابعة": "Fourth Year",
    "سنة خامسة": "Fifth Year", "السنة الخامسة": "Fifth Year",
    "فصل أول": "Fall Semester", "الفصل الأول": "Fall Semester", "فصل اول": "Fall Semester",
    "فصل ثاني": "Spring Semester", "الفصل الثاني": "Spring Semester",
    "الخطة": "program of study course distribution", "خطة": "program of study course distribution",
    "صدرت": "version", "صدور": "version", "إصدار": "version", "اصدار": "version",
    "نسخة": "version",
    "متطلبات التخرج": "graduation awarded", "شروط التخرج": "graduation awarded",
    "متطلب": "prerequisite corequisite", "متطلبات": "prerequisite requirements",
    "أول سنة": "First Year", "اول سنة": "First Year", "ثاني سنة": "Second Year",
    "ثالث سنة": "Third Year", "رابع سنة": "Fourth Year", "خامس سنة": "Fifth Year",
    "سنوات": "year", "سنين": "year", "مدة الدراسة": "year",
    "المعالجات": "Microprocessors", "معالجات": "Microprocessors", "الدوائر": "Electric Circuits",
    "مختبر": "lab laboratory", "المختبر": "lab laboratory", "عملي": "lab laboratory",
    "مواد حرة": "free courses", "مساقات حرة": "free courses", "حرة": "free courses",
    "أهداف": "objectives", "اهداف": "objectives", "مخرجات التعلم": "learning outcomes",
    "مشروع البرمجيات": "Software Project", "مشروع برمجيات": "Software Project",
    "فصل": "semester", "الفصل": "semester", "سنة": "year", "السنة": "year",
    "شروط القبول": "admission requirements minimum average Tawjihi",
    "معدل القبول": "admission requirements minimum average Tawjihi",
    "وصف": "course description", "بنوخذ": "course description topics", "بناخذ": "course description topics",
    "نظم التشغيل": "Operating Systems", "أنظمة التشغيل": "Operating Systems",
    "شبكات الحاسوب": "Computer Networks", "الشبكات": "Computer Networks",
    "هياكل البيانات": "Data Structures", "تراكيب البيانات": "Data Structures",
    "قواعد البيانات": "Database Systems", "قاعدة البيانات": "Database Systems",
    "مادة البرمجة": "Programming Fundamentals for Engineers",
    "أساسيات البرمجة": "Programming Fundamentals for Engineers",
    "اساسيات البرمجة": "Programming Fundamentals for Engineers",
    "مبادئ البرمجة": "Programming Fundamentals for Engineers",
    "البرمجة الكينونية": "Object-Oriented Programming", "البرمجة الشيئية": "Object-Oriented Programming",
    "هندسة البرمجيات": "Software Engineering",
    "المعالجات الدقيقة": "Microprocessors", "المايكروبروسيسور": "Microprocessors",
    "الأنظمة المدمجة": "Embedded Systems", "الانظمة المدمجة": "Embedded Systems",
    "الدوائر الكهربائية": "Electric Circuits", "الالكترونيات": "Electronics",
    "الإلكترونيات": "Electronics",
    "التصميم المنطقي": "Digital Logic Systems", "الدوائر المنطقية": "Digital Logic Systems",
    "الأنظمة الرقمية": "Digital Logic Systems",
    "تنظيم الحاسوب": "Computer Organization & Architecture",
    "معمارية الحاسوب": "Computer Organization & Architecture",
    "الرياضيات المتقطعة": "Discrete Math", "الاحتمالات": "Probability & Statistics",
    "الإحصاء": "Probability & Statistics", "التفاضل والتكامل": "Calculus",
    "تفاضل": "Calculus", "الفيزياء": "Physics", "فيزياء": "Physics",
    "أمن الشبكات": "Networks Security", "امن الشبكات": "Networks Security",
    "الرسم الهندسي": "Engineering Drawing", "الرسومات الحاسوبية": "Computer Graphics",
    "الجرافيكس": "Computer Graphics", "تطبيقات الويب": "Building Web Applications",
    "الوسائط المتعددة": "Multimedia Fundamentals", "الملتيميديا": "Multimedia Fundamentals",
    "اتصال البيانات": "Data Communication Systems", "اتصالات البيانات": "Data Communication Systems",
    "الشبكات اللاسلكية": "Wireless Networking", "التحليل العددي": "Engineering Numerical Analysis",
    "معالجة الإشارات": "Digital Signal Processing", "الرياضيات الهندسية": "Engineering Mathematics",
    "رياضيات هندسية": "Engineering Mathematics", "الديناميكا الحرارية": "Thermodynamics",
    "الإدارة الهندسية": "Engineering Management", "الكتابة الفنية": "Technical Writing",
}
_GLOSSARY_NORM = [(normalize_for_search(k), v) for k, v in ACADEMIC_GLOSSARY.items()]


def academic_terms(query: str) -> str:
    """English academic terms for an Arabic question (empty when nothing matches)."""
    q = normalize_for_search(query)
    return " ".join(dict.fromkeys(v for k, v in _GLOSSARY_NORM if k in q))


def expand_query(query: str) -> str:
    extra = " ".join(x for x in (dialect_to_msa(query), academic_terms(query)) if x)
    return f"{query} ({extra})" if extra else query


# --- Metadata extraction ----------------------------------------------------------
_ORDINALS = {
    "الأولى": "1", "الاولى": "1", "الثانية": "2", "الثالثة": "3", "الرابعة": "4",
    "الخامسة": "5", "السادسة": "6", "السابعة": "7", "الثامنة": "8", "التاسعة": "9",
    "العاشرة": "10",
}
# 1-3 digits only: «المادة 2003» is a year / reference, not an article number.
_ARTICLE_RE = re.compile(
    r"(?:^|\s)(?:المادة|مادة)\s*[\(\[]?\s*([0-9٠-٩]{1,3}|" + "|".join(_ORDINALS) + r")"
    r"(?![0-9٠-٩])\s*[\)\]]?"
    r"|\bArticle\s+(\d{1,3})\b",
    re.IGNORECASE,
)


def find_article_number(text: str) -> str:
    """Return the first explicit article number (e.g. 'المادة (12)') or ''."""
    m = _ARTICLE_RE.search(text)
    if not m:
        return ""
    val = m.group(1) or m.group(2) or ""
    return _ORDINALS.get(val, val.translate(_ARABIC_DIGITS))


_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*[/\-–]\s*(20\d{2})(?!\d)")


def find_academic_year(text: str) -> str:
    """Return an explicitly written academic year such as '2026/2027', else ''."""
    m = _YEAR_RE.search(text.translate(_ARABIC_DIGITS))
    if not m:
        return ""
    a, b = int(m.group(1)), int(m.group(2))
    if abs(a - b) == 1:
        lo, hi = sorted((a, b))
        return f"{lo}/{hi}"
    return ""
