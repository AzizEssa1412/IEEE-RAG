"""End-to-end: upload -> index -> route -> retrieve -> answer with citations, plus API rules."""
import pytest
from fastapi.testclient import TestClient

from app import db, vectorstore
from app.config import COLLECTION_HACKATHON
from app.main import app
from app.rag.answer import answer
from app.rag.router import route

ADMIN = {"X-Admin-Token": "test-admin"}


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def indexed(client, synthetic_university, fixtures_dir):
    ids = {}
    for name in ("SYNTHETIC_regulations_for_testing.txt", "SYNTHETIC_courses_for_testing.csv"):
        with open(fixtures_dir / name, "rb") as fh:
            r = client.post("/api/sources/upload", headers=ADMIN,
                            files={"file": (name, fh)},
                            data={"university_id": "synthetic_u", "target": "uploads",
                                  "version": "synthetic-v1", "academic_year": "2026/2027"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "indexed"
        ids[name] = r.json()["id"]
    return ids


# --- routing -------------------------------------------------------------------------
def test_router_rules(synthetic_university):
    assert route("شو شروط التسجيل بجامعة القدس؟").university["id"] == "alquds"
    assert route("ما شروط التخرج؟").status == "ask"
    assert route("ما شروط التخرج؟", "alquds").university["id"] == "alquds"
    r = route("شو شروط القبول في جامعة القدس المفتوحة؟", "alquds")
    assert r.status == "unknown_university" and "المفتوحة" in r.mentioned[0]
    assert route("كم ساعة بجامعة بيرزيت؟", "alquds").status == "unknown_university"
    assert route("قارن بين جامعة القدس وجامعة اصطناعية للاختبار").status == "multiple"


def test_unknown_university_question_asks_first():
    out = answer("كم ساعة بقدر أنزل بالفصل؟")
    assert out["status"] == "needs_university" and out["citations"] == []


# --- grounded answers -----------------------------------------------------------------
def test_answer_cites_real_passage_and_file(indexed):
    out = answer("كم ساعة بقدر أنزل بالفصل؟", "synthetic_u")
    assert out["status"] == "answered", out
    assert any("18 ساعة" in c["passage"] for c in out["citations"])
    cit = next(c for c in out["citations"] if "18 ساعة" in c["passage"])
    assert cit["university_id"] == "synthetic_u"
    assert cit["file_ref"] == "SYNTHETIC_regulations_for_testing.txt"
    assert cit["article"] == "5"
    assert cit["version"] == "synthetic-v1"
    assert cit["link"].startswith(f"/api/documents/{cit['doc_id']}/file")


def test_course_code_question_uses_structured_record(indexed):
    out = answer("شو المتطلب السابق لمساق 9990201؟", "synthetic_u")
    assert out["status"] == "answered"
    rec = next(c for c in out["citations"] if c["kind"] == "course_record")
    assert "9990102" in rec["passage"] and "9990202" in rec["passage"]


def test_absent_answer_is_refused(indexed):
    out = answer("ما هو لون شعار نادي كرة السلة؟", "synthetic_u")
    assert out["status"] == "no_answer" and out["citations"] == []


def test_universities_are_never_mixed(indexed):
    # Al-Quds has no documents in the test DB: the synthetic regulation must NOT leak in.
    out = answer("كم ساعة بقدر أنزل بالفصل بجامعة القدس؟")
    assert out["status"] == "no_answer"
    assert all(c["university_id"] == "alquds" for c in out["citations"])
    hits = vectorstore.vector_search(["synthetic_u_uploaded_academic_documents",
                                      COLLECTION_HACKATHON], "alquds", "ساعة معتمدة", k=5)
    assert hits == []


# --- API --------------------------------------------------------------------------------
def test_admin_endpoints_require_token(client):
    assert client.post("/api/ingest/website", json={}).status_code == 401
    assert client.delete("/api/sources/1").status_code == 401


def test_add_url_rejects_foreign_domains_and_portals(client):
    r = client.post("/api/sources/url", headers=ADMIN,
                    json={"university_id": "alquds", "url": "https://example.com/ar/"})
    assert r.status_code == 400
    r = client.post("/api/sources/url", headers=ADMIN,
                    json={"university_id": "alquds", "url": "https://www.alquds.edu/wp-login.php"})
    assert r.status_code == 400


def test_sources_listing_stats_and_file_download(client, indexed):
    docs = client.get("/api/sources", params={"university_id": "synthetic_u"}).json()
    assert {d["file_ref"] for d in docs} >= {"SYNTHETIC_courses_for_testing.csv"}
    stats = client.get("/api/sources/stats").json()
    assert stats["hackathon_dataset_present"] is False
    r = client.get(f"/api/documents/{indexed['SYNTHETIC_regulations_for_testing.txt']}/file")
    assert r.status_code == 200 and "18 ساعة".encode() in r.content
    course = client.get("/api/courses/9990102", params={"university_id": "synthetic_u"}).json()
    assert {d["code"] for d in course[0]["required_by"]} == {"9990201"}


def test_duplicate_upload_is_detected(client, indexed, fixtures_dir):
    name = "SYNTHETIC_courses_for_testing.csv"
    with open(fixtures_dir / name, "rb") as fh:
        r = client.post("/api/sources/upload", headers=ADMIN, files={"file": ("copy.csv", fh)},
                        data={"university_id": "synthetic_u"})
    assert r.json()["status"] == "duplicate"


def test_delete_removes_document_and_embeddings(client, synthetic_university, tmp_path):
    p = tmp_path / "SYNTHETIC_delete_me.txt"
    p.write_text("نص اصطناعي مؤقت للحذف حول رسوم المواقف الاصطناعية.", encoding="utf-8")
    with open(p, "rb") as fh:
        doc_id = client.post("/api/sources/upload", headers=ADMIN, files={"file": (p.name, fh)},
                             data={"university_id": "synthetic_u"}).json()["id"]
    col = synthetic_university["uploads_collection"]
    assert vectorstore.count(col, {"doc_id": doc_id}) > 0
    assert client.delete(f"/api/sources/{doc_id}", headers=ADMIN).status_code == 200
    assert vectorstore.count(col, {"doc_id": doc_id}) == 0
    assert db.get_document(doc_id) is None
