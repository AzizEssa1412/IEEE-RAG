"""Test configuration: isolated temp data dir, offline embeddings, no LLM.

Environment variables must be set BEFORE any `app` module is imported, because the
settings object is built at import time.
"""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="uniguide-test-")
os.environ["UNIGUIDE_DATA_DIR"] = _TMP
os.environ["UNIGUIDE_FAKE_EMBEDDINGS"] = "1"     # deterministic offline embedder
os.environ["EMBEDDING_MODEL"] = "fake-hash-embedder"
os.environ["LLM_PROVIDER"] = "none"              # extractive mode: no network, no keys
os.environ["MIN_SIMILARITY"] = "0.30"            # calibrated for the fake embedder
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["DEFAULT_UNIVERSITY"] = ""             # tests cover the "which university?" flow
os.environ["CRAWL_DELAY_SECONDS"] = "0"

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Clearly labelled SYNTHETIC institution used only by tests (never by the real app).
SYNTHETIC_UNI = {
    "id": "synthetic_u",
    "name_ar": "جامعة اصطناعية للاختبار",
    "name_en": "Synthetic Test University",
    "aliases": ["جامعة اصطناعية للاختبار", "synthetic test university"],
    "domains": ["synthetic.example"],
}


@pytest.fixture(scope="session")
def synthetic_university():
    from app import db

    if not db.get_university(SYNTHETIC_UNI["id"]):
        db.add_university(SYNTHETIC_UNI["id"], SYNTHETIC_UNI["name_ar"], SYNTHETIC_UNI["name_en"],
                          SYNTHETIC_UNI["aliases"], SYNTHETIC_UNI["domains"])
    return db.get_university(SYNTHETIC_UNI["id"])


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES
