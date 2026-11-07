"""Answerability evaluation against the live API.

Each question is labelled by hand from the indexed CE study plan
(AcademicPlan-AQU-Eng-CE2010.pdf):
  True  = the plan contains the answer -> the assistant must answer
  False = the plan does not contain it -> the assistant must refuse

Run:  python -m eval.answerability [--base http://127.0.0.1:8000] [-v]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

CASES: list[tuple[str, bool]] = [
    # ---- answerable: stated in the plan ------------------------------------------------
    ("شو معدل القبول بهندسة الحاسوب؟", True),                       # p2: 80% scientific stream
    ("كم ساعة لازم أخلص عشان أتخرج؟", True),                        # p3: 166 credit hours
    ("كم ساعة متطلبات الجامعة؟", True),                             # p3/p4: 24
    ("كم ساعة متطلبات الكلية؟", True),                              # p5: 53
    ("كم ساعة مواد حرة لازم آخذ؟", True),                            # p5: 6 free credits
    ("كم مادة اختيارية من متطلبات التخصص لازم أختار؟", True),         # p7: three courses (9 credits)
    ("كم ساعة تدريب ميداني لازم؟", True),                           # p6: 135 hours
    ("شو المتطلب السابق لمادة 0702211؟", True),                     # p6: 0702113
    ("شو المتطلب السابق لمادة الشبكات؟", True),                     # p6: 0702451
    ("إذا رسبت بمادة البرمجة، بقدر أنزل اللي بعدها؟", True),          # dependents of 0702113
    ("شو بنوخذ في مادة نظم التشغيل؟", True),                        # p17 description
    ("شو مواد سنة أولى فصل أول؟", True),                            # p8
    ("شو مواد سنة رابعة فصل ثاني؟", True),                          # p8
    ("شو المواد الاختيارية؟", True),                                # tables I-b, I-c, II-b, III-b
    ("شو متطلبات التسجيل في مشروع التخرج؟", True),                  # p6/p20
    ("كم ساعة لازم أكون مخلص عشان أسجل مشروع البرمجيات؟", True),      # Note 1: 110 hours
    ("شو أهداف برنامج هندسة الحاسوب؟", True),                      # p2 objectives
    ("كم ساعة معتمدة مادة قواعد البيانات؟", True),                  # 0702324: 3
    ("What is the prerequisite of Embedded Systems?", True),        # 0701342
    ("شو وصف مادة الرياضيات المتقطعة؟", True),                      # p15
    ("متى صدرت الخطة الدراسية؟", True),                             # March 2010
    ("هل مادة الرسم الهندسي إجبارية؟", True),                       # Table II-a compulsory
    # ---- not answerable: not in the plan ----------------------------------------------
    ("كم سعر الساعة المعتمدة؟", False),
    ("إمتى فترة الحذف والإضافة؟", False),
    ("إمتى آخر موعد للتسجيل؟", False),
    ("مين رئيس قسم هندسة الحاسوب؟", False),
    ("شو رقم تلفون القسم؟", False),
    ("كم الحد الأعلى للساعات اللي بقدر أسجلها بالفصل؟", False),
    ("كيف بقدر أسحب مادة؟", False),
    ("في سكن للطلاب بالجامعة؟", False),
    ("شو المنح المتوفرة لطلاب الهندسة؟", False),
    ("شو المتطلب السابق لمادة تعلم الآلة؟", False),
    ("شو بنوخذ في مادة الذكاء الاصطناعي؟", False),
    ("كم معدل التخرج المطلوب؟", False),
    ("في ماجستير هندسة حاسوب بالجامعة؟", False),
    ("كيف بحول من جامعة ثانية لهندسة الحاسوب؟", False),
    ("إمتى بتبلش امتحانات النهائي؟", False),
    ("شو اسم رئيس نادي الشطرنج وكم سعر الفلافل؟", False),
    ("شو أحسن لابتوب لطالب هندسة حاسوب؟", False),
    ("شو شروط التخرج بجامعة بيرزيت؟", False),
    ("What is the prerequisite of Machine Learning?", False),
    ("كم عدد طلاب هندسة الحاسوب؟", False),
]


def ask(base: str, q: str) -> dict:
    req = urllib.request.Request(f"{base}/api/chat", data=json.dumps({"question": q}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=240))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    tp = tn = fp = fn = 0
    for q, should in CASES:
        r = ask(args.base, q)
        answered = r["status"] in ("answered", "conflict")
        ok = answered == should
        tp += should and answered
        tn += (not should) and (not answered)
        fp += (not should) and answered
        fn += should and (not answered)
        d = r.get("debug", {})
        mark = "OK  " if ok else "FAIL"
        print(f"{mark} {'ANS' if should else 'REF'} -> {r['status']:<10} sim={d.get('best_similarity')} "
              f"why={d.get('evidence', '')} | {q}")
        if args.verbose and not ok:
            print("      ", r["answer"][:200].replace("\n", " / "))
    n = len(CASES)
    print(f"\naccuracy {(tp + tn) / n:.0%}  ({tp + tn}/{n})  answered-correctly {tp}  "
          f"refused-correctly {tn}  wrongly-answered {fp}  wrongly-refused {fn}")
    return 0 if fp == 0 and fn == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
