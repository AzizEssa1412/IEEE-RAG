import json, urllib.request
Q = ["مرحبا", "شو هي المواد بالسنة الثانية؟", "بدي أعرف عن مادة الدوائر الكهربائية", "شو متطلبات مادة قواعد البيانات؟",
     "ايش المواد الاجبارية؟", "كم مادة بالخطة؟", "كم سنة الدراسة؟", "شو المواد اللي بنوخذها بأول سنة؟",
     "شو المادة اللي لازم أخلصها قبل البرمجة الكينونية؟", "متطلب مادة المعالجات", "مواد الفصل الثاني سنة ثالثة",
     "شو هي مادة Data Structures؟", "prerequisite of operating systems", "شو مادة 0702451", "كم ساعة الخطة كلها؟",
     "كيف بتكون الخطة الدراسية؟", "شو متطلبات التخرج؟", "شو أهداف التخصص؟", "معدل القبول", "شو المواد الحرة؟",
     "شو مادة فيزياء 1؟", "متى بقدر آخذ مشروع التخرج؟", "شو مادة الشبكات بتعلمنا؟", "electives", "قديش ساعات التدريب؟"]
for q in Q:
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/api/chat", data=json.dumps({"question": q, "university_id": "alquds"}).encode(), headers={"Content-Type": "application/json"}), timeout=240))
    print(f"{r['status']:<10} {str(r.get('debug',{}).get('evidence')):<28} | {q} || {r['answer'][:90]!r}")
