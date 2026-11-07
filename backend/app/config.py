"""Central configuration. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(_env("UNIGUIDE_DATA_DIR", str(ROOT_DIR / "data")))
    admin_token: str = _env("ADMIN_TOKEN", "change-me-admin")

    # Embeddings (free, local, multilingual, good Arabic coverage)
    embedding_model: str = _env("EMBEDDING_MODEL", "BAAI/bge-m3")

    # LLM providers: "auto" tries Gemini (if a key is set) then Ollama, else extractive mode.
    llm_provider: str = _env("LLM_PROVIDER", "auto")
    gemini_api_key: str = _env("GEMINI_API_KEY", _env("GOOGLE_API_KEY", ""))
    gemini_model: str = _env("GEMINI_MODEL", "gemini-2.5-flash")
    ollama_url: str = _env("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = _env("OLLAMA_MODEL", "qwen2.5:7b")

    # Polite crawling
    user_agent: str = _env(
        "CRAWL_USER_AGENT",
        "UniGuideAI/1.0 (IEEE RAG Hackathon academic assistant; respects robots.txt)",
    )
    crawl_delay_seconds: float = float(_env("CRAWL_DELAY_SECONDS", "1.5"))
    request_timeout_seconds: float = float(_env("REQUEST_TIMEOUT_SECONDS", "30"))
    max_pages_per_run: int = int(_env("MAX_PAGES_PER_RUN", "45"))
    max_pdfs_per_run: int = int(_env("MAX_PDFS_PER_RUN", "25"))
    max_pdf_bytes: int = int(_env("MAX_PDF_BYTES", str(25 * 1024 * 1024)))

    # Chunking
    chunk_chars: int = int(_env("CHUNK_CHARS", "900"))
    chunk_overlap: int = int(_env("CHUNK_OVERLAP", "150"))

    # This deployment serves Al-Quds University Computer Engineering students: questions that
    # name no university go to this one (other named universities are still refused).
    default_university: str = _env("DEFAULT_UNIVERSITY", "alquds")

    # Retrieval
    top_k: int = int(_env("TOP_K", "6"))
    # Floor for "the passages are in the right area at all" (the subject itself is checked by
    # term coverage, see rag/evidence.py). The scale depends on the embedding model; calibrated
    # with `python -m eval.calibrate` on the indexed CE plan:
    #   bge-m3   : answerable 0.506-0.70; unrelated 0.23 (chess club), 0.35 (bus); a
    #              CE-flavoured off-topic one (laptop, 0.595) is caught by coverage  -> 0.48
    #   e5-small : answerable 0.82-0.91, unrelated 0.76                               -> 0.83
    min_similarity: float = float(_env(
        "MIN_SIMILARITY", "0.83" if "e5" in _env("EMBEDDING_MODEL", "BAAI/bge-m3").lower() else "0.48"))

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "uniguide.db"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def files_dir(self) -> Path:
        """Downloaded/uploaded originals that PDF citations link to."""
        return self.data_dir / "files"


settings = Settings()
for _p in (settings.data_dir, settings.chroma_dir, settings.files_dir):
    _p.mkdir(parents=True, exist_ok=True)

# Collections required by the specification. Keeping them physically separate
# (plus a university_id metadata filter) guarantees sources are never mixed.
COLLECTION_HACKATHON = "hackathon_official_documents"
COLLECTION_ALQUDS_WEB = "alquds_official_website"
COLLECTION_ALQUDS_UPLOADS = "alquds_uploaded_academic_documents"
ALL_COLLECTIONS = (COLLECTION_HACKATHON, COLLECTION_ALQUDS_WEB, COLLECTION_ALQUDS_UPLOADS)
