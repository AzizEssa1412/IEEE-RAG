"""Similarity calibration through the API (start the server with MIN_SIMILARITY=0 LLM_PROVIDER=none).

Prints the best-similarity distribution for answerable vs unanswerable questions so the
in-area floor (MIN_SIMILARITY) can be set for the current embedding model.
"""
import json
import urllib.request

from eval.answerability import CASES
from eval.holdout_cases import HOLDOUT

ans, ref = [], []
for q, should in CASES + HOLDOUT:
    req = urllib.request.Request("http://127.0.0.1:8000/api/chat", data=json.dumps({"question": q}).encode(),
                                 headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=240))
    sim = r.get("debug", {}).get("best_similarity")
    if sim is not None:
        (ans if should else ref).append((sim, r["debug"]["evidence"], q))
ans.sort(); ref.sort()
print("ANSWERABLE   min %.4f  median %.4f" % (ans[0][0], ans[len(ans) // 2][0]))
print("UNANSWERABLE max %.4f  median %.4f" % (ref[-1][0], ref[len(ref) // 2][0]))
print("lowest answerable:", ans[:4])
print("unanswerable passing coverage:", [x for x in ref if x[1] == "covered"])
print("answerable failing coverage:", [x for x in ans if x[1] != "covered"])
