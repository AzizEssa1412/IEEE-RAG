"""The four acceptance questions: shows answer, retrieved passages and citations."""
import json
import sys
import urllib.request

QUESTIONS = ["شو شروط الانسحاب من مادة؟", "كم ساعة بقدر أسجل؟", "شو بصير إذا رسبت بمادة؟", "شو شروط التخرج؟"]
base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
for q in QUESTIONS:
    req = urllib.request.Request(f"{base}/api/chat", data=json.dumps({"question": q}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=600))
    print("=" * 100)
    print("Q:", q)
    print("status:", r["status"], "| mode:", r.get("mode"), "| debug:", r.get("debug"))
    print("ANSWER:\n" + r["answer"])
    for c in r.get("citations", []):
        print(f"  [{c['n']}] {c['title']} | page {c.get('page')} | {c.get('link') or c.get('url')}")
        print("      passage:", c["passage"][:300].replace("\n", " / "))
