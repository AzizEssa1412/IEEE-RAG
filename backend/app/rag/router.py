"""Decide WHICH university a question is about — before any retrieval happens.

Rules (from the specification):
  * never mix universities in one answer
  * if the university is unknown, ask the student which one they mean
  * a university we hold no authorised documents for gets an honest refusal
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import db
from ..config import settings
from ..text.arabic import normalize_for_search

# Institutions whose names contain a known university's name but are DIFFERENT.
CONFUSABLES = {
    "جامعه القدس المفتوحه": "جامعة القدس المفتوحة",
    "القدس المفتوحه": "جامعة القدس المفتوحة",
    "al-quds open university": "جامعة القدس المفتوحة (Al-Quds Open University)",
    "al quds open university": "جامعة القدس المفتوحة (Al-Quds Open University)",
    "qou": "جامعة القدس المفتوحة (QOU)",
}
# A *named* university: «جامعة X» possibly with a proclitic (بجامعة / لجامعة / وجامعة) but NOT
# the definite «الجامعة» / «بالجامعة», which just means "the university" (generic).
_OTHER_UNI = re.compile(
    r"(?:^|(?<=\s))(?:[وف]?[بلك]?)جامع[هة]\s+((?:ال)?[ء-ي]{2,}(?:\s+ال[ء-ي]{2,})?)")
_GENERIC_AFTER = {"ما", "في", "من", "على", "عن", "او", "و", "هي", "هو"}


@dataclass
class Route:
    status: str                      # ok | ask | multiple | unknown_university
    university: dict | None = None
    mentioned: list[str] = field(default_factory=list)


def _contains(haystack: str, needle: str) -> bool:
    """Whole-word match that tolerates attached Arabic proclitics (و ف ب ل ك), e.g. «بجامعة»."""
    prefix = r"(?:^|(?<=[\s\W]))(?:[وف]?[بلك]?)" if re.match(r"[؀-ۿ]", needle) \
        else r"(?<![\w])"
    return bool(re.search(prefix + re.escape(needle) + r"(?![\w])", haystack))


def _alias_hits(qn: str, universities: list[dict]) -> list[dict]:
    hits = []
    for u in universities:
        for alias in u["aliases"] + [u["name_ar"], u["name_en"]]:
            a = normalize_for_search(alias)
            if a and _contains(qn, a):
                hits.append(u)
                break
    return hits


def route(question: str, selected_id: str | None = None) -> Route:
    universities = db.list_universities()
    qn = normalize_for_search(question)

    unknown: list[str] = []
    for key, label in CONFUSABLES.items():
        k = normalize_for_search(key)
        if _contains(qn, k):
            if label not in unknown:
                unknown.append(label)
            qn = qn.replace(k, " ")

    hits = _alias_hits(qn, universities)

    known_norm = [normalize_for_search(a) for u in universities
                  for a in u["aliases"] + [u["name_ar"]]]
    for m in _OTHER_UNI.finditer(qn):
        first_word = m.group(1).split()[0]
        if first_word in _GENERIC_AFTER or first_word.startswith("و"):
            continue
        full = normalize_for_search("جامعة " + m.group(1))
        if any(full.startswith(k) or k.startswith(full) for k in known_norm if k.startswith("جامع")):
            continue
        unknown.append("جامعة " + m.group(1))

    if len(hits) > 1:
        return Route("multiple", mentioned=[u["name_ar"] for u in hits])
    if len(hits) == 1:
        return Route("ok", hits[0])
    if unknown:
        return Route("unknown_university", mentioned=list(dict.fromkeys(unknown)))
    for uid in (selected_id, settings.default_university):
        u = next((x for x in universities if x["id"] == uid), None) if uid else None
        if u:
            return Route("ok", u)
    return Route("ask")


def strip_university_names(question: str, university: dict) -> str:
    """Remove the (already routed) university's name so it doesn't dominate retrieval."""
    out = question
    for alias in sorted(university["aliases"] + [university["name_ar"]], key=len, reverse=True):
        if len(alias) < 4:
            continue
        out = re.sub(r"(?:[وف]?[بلك]?)" + re.escape(alias), " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip()
    return out if len(out) >= 4 else question
