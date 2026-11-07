"""Runs the hackathon demonstration questions against the REAL index.

Nothing is hard-coded: every answer, status and citation printed here is produced by
the live pipeline (router -> retriever -> answer) over the indexed official sources.

  python -m scripts.demo            # human-readable report
  python -m scripts.demo --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.rag.answer import answer  # noqa: E402

SCENARIOS = [
    ("1. سؤال عربي من صفحة رسمية على الموقع", "ما هي مسؤوليات الطالب حسب دائرة التسجيل في جامعة القدس؟", None),
    ("2. سؤال أكاديمي من ملف PDF", "ما هو المتطلب السابق لمساق Data Structures 0702211 في خطة هندسة الحاسوب؟", "alquds"),
    ("3. إجابة تستشهد بعدة مقاطع", "كم عدد الساعات المعتمدة المطلوبة للتخرج في خطة هندسة الحاسوب وكيف تتوزع؟", "alquds"),
    ("4. سؤال باللهجة الفلسطينية", "وين بلاقي دليل القبول؟", "alquds"),
    ("4b. لهجة", "كيف بطلع وثيقة تخرج من التسجيل؟", "alquds"),
    ("5. سؤال إجابته غير موجودة (رفض صحيح)", "شو اسم رئيس نادي الشطرنج بالجامعة وكم سعر الفلافل بالكافتيريا؟", "alquds"),
    ("6a. اختيار الجامعة الصحيحة", "شو شروط القبول في جامعة القدس المفتوحة؟", "alquds"),
    ("6b. جامعة غير محددة", "كم ساعة بقدر أنزل بالفصل؟", None),
    ("6c. جامعة غير مفهرسة", "شو شروط التخرج بجامعة بيرزيت؟", "alquds"),
    ("7. مرجع قابل للنقر", "شو الحد الأدنى لمعدل القبول في هندسة الحاسوب بجامعة القدس؟", None),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = []
    for label, q, uni in SCENARIOS:
        res = answer(q, uni)
        out.append({"scenario": label, "question": q, "selected_university": uni, **res})
        if a.json:
            continue
        print("=" * 100)
        print(f"{label}\nسؤال: {q}   (الجامعة المختارة: {uni or '—'})")
        print(f"الحالة: {res['status']}   الوضع: {res.get('mode', '—')}   "
              f"تشخيص: {res.get('debug', {})}")
        print("-" * 100)
        print(res["answer"][:1500])
        for c in res.get("citations", []):
            loc = f"صفحة {c['page']}" if c.get("page") else ""
            print(f"  [{c['n']}] {c['university']} – {c['title']} | {c['source_type']} {loc} "
                  f"| المادة {c['article'] or '—'} | الرابط: {c['link']} | تاريخ الاطلاع: "
                  f"{c['retrieved_at']}")
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
