"""Expected outcomes (policy, broken site links, link-only pages) are skips, not errors."""
from fastapi.testclient import TestClient

from app import db
from app.ingest.pipeline import _fetch_problem, _index_page
from app.ingest.web import FetchResult, PageContent
from app.main import app


def _doc(uni, url, origin="discovered"):
    return db.upsert_document(university_id=uni["id"], collection=uni["web_collection"],
                              source_type="website", url=url, origin=origin, title=url)


def _errors():
    return TestClient(app).get("/api/errors").json()


def test_policy_and_broken_discovered_links_are_skipped(synthetic_university):
    uni = synthetic_university
    run = db.start_run("website", uni["id"])
    pay = _doc(uni, "https://synthetic.example/pay")
    dead = _doc(uni, "https://synthetic.example/dead")
    seed = _doc(uni, "https://synthetic.example/seed-dead", origin="seed")
    assert _fetch_problem(pay, run, FetchResult(url="", ok=False, stage="policy",
                                                error="private URL skipped"), "u", "discovered") == "skipped"
    assert _fetch_problem(dead, run, FetchResult(url="", ok=False, error="HTTP 404"),
                          "https://synthetic.example/dead", "discovered") == "skipped"
    # A seed URL that 404s is a real problem the admin must see.
    assert _fetch_problem(seed, run, FetchResult(url="", ok=False, error="HTTP 404"),
                          "https://synthetic.example/seed-dead", "seed") == "failed"
    assert db.get_document(pay)["status"] == "skipped"
    urls = [e["url"] for e in _errors()]
    assert "https://synthetic.example/seed-dead" in urls
    assert "https://synthetic.example/dead" not in urls


def test_link_only_page_is_skipped_and_errors_are_deduplicated(synthetic_university):
    uni = synthetic_university
    hub = _doc(uni, "https://synthetic.example/guides")
    pc = PageContent(title="أدلة", blocks=[], links=[],
                     pdf_links=[("https://synthetic.example/g.pdf", "دليل القبول")])
    out = _index_page(hub, uni, pc, "https://synthetic.example/guides", None, set(), db.now_iso())
    assert out["status"] == "skipped" and db.get_document(hub)["status"] == "skipped"

    empty = _doc(uni, "https://synthetic.example/js-only")
    for _ in range(3):  # three runs, same failure
        run = db.start_run("website", uni["id"])
        pc = PageContent(title="", blocks=[], links=[], pdf_links=[])
        _index_page(empty, uni, pc, "https://synthetic.example/js-only", run, set(), db.now_iso())
    assert [e["url"] for e in _errors()].count("https://synthetic.example/js-only") == 1
