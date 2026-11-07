"""ChromaDB persistence + free local multilingual embeddings (sentence-transformers).

Every chunk carries full provenance metadata. Searches are ALWAYS restricted to one
university: only that university's collections are opened, and a `university_id`
metadata filter is applied as a second, independent guard.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Any, Iterable

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

import chromadb  # noqa: E402
from chromadb.config import Settings as ChromaSettings

from .config import settings

_lock = threading.RLock()
_client = None
_model = None
_version = 0  # bumped on every write; lets the lexical index know when to rebuild


def data_version() -> int:
    return _version


def _bump() -> None:
    global _version
    _version += 1


class VectorStoreBusy(RuntimeError):
    pass


_lock_handle = None


def _acquire_process_lock() -> None:
    """ChromaDB's on-disk index must have ONE writer/reader process.

    A second process opening the same directory (a script run while the API server is up)
    leaves the server's in-memory HNSW index out of step with disk: passages silently stop
    being found. An OS-level exclusive lock makes that mistake fail loudly instead.
    """
    global _lock_handle
    if _lock_handle is not None:
        return
    settings.chroma_dir.mkdir(parents=True, exist_ok=True)
    fh = open(settings.chroma_dir / ".uniguide.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise VectorStoreBusy(
            "قاعدة المتجهات مفتوحة من عملية أخرى (غالباً خادم الـ API). أوقف الخادم أولاً، أو "
            "نفّذ العملية عبر الـ API. / The vector store is open in another process "
            "(usually the API server): stop it first or use the API.") from e
    _lock_handle = fh


def client():
    global _client
    with _lock:
        if _client is None:
            _acquire_process_lock()
            _client = chromadb.PersistentClient(
                path=str(settings.chroma_dir),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return _client


def reset_client() -> None:
    global _client, _lock_handle
    _client = None
    _checked.clear()
    if _lock_handle is not None:
        _lock_handle.close()          # closing the handle releases the OS lock
        _lock_handle = None


_checked: set[str] = set()


def collection(name: str):
    """The collection, rebuilt empty if it was built with a different embedding model.

    Vectors from different models are not comparable (bge-m3: 1024 dims, e5-small: 384);
    mixing them makes every search wrong. Documents in a rebuilt collection must be
    re-indexed (`python -m scripts.ingest reindex`).
    """
    store = client()
    meta = {"hnsw:space": "cosine", "embedding_model": settings.embedding_model}
    col = store.get_or_create_collection(name=name, metadata=meta)
    if name not in _checked:
        built_with = (col.metadata or {}).get("embedding_model")
        if built_with != settings.embedding_model and col.count() > 0:
            logging.getLogger("uniguide.vectorstore").warning(
                "collection %s was built with %s, now %s: rebuilding (re-index needed)",
                name, built_with, settings.embedding_model)
            store.delete_collection(name)
            col = store.get_or_create_collection(name=name, metadata=meta)
            _bump()
        elif built_with != settings.embedding_model:
            # empty collection: recreate it with the current model recorded (Chroma does not
            # allow modifying metadata that carries the distance function)
            store.delete_collection(name)
            col = store.get_or_create_collection(name=name, metadata=meta)
        _checked.add(name)
    return col


def existing_collections() -> set[str]:
    store = client()      # VectorStoreBusy must surface, never look like "no collections"
    try:
        return {c if isinstance(c, str) else c.name for c in store.list_collections()}
    except Exception:  # pragma: no cover
        return set()


# --- Embeddings -------------------------------------------------------------------
class _FakeEmbedder:
    """Deterministic bag-of-words hashing embedder for offline unit tests only."""

    dim = 256

    def encode(self, texts: list[str], **_: Any) -> list[list[float]]:
        from .text.arabic import tokenize

        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in tokenize(t):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


def embedder():
    global _model
    with _lock:
        if _model is None:
            if os.environ.get("UNIGUIDE_FAKE_EMBEDDINGS") == "1":
                _model = _FakeEmbedder()
            else:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(settings.embedding_model, device="cpu")
                # bge-m3 accepts 8192 tokens; chunks are ~900 characters, 512 keeps it fast
                _model.max_seq_length = min(_model.max_seq_length or 512, 512)
        return _model


def _is_e5() -> bool:
    return "e5" in settings.embedding_model.lower()


def embed_passages(texts: list[str]) -> list[list[float]]:
    prefix = "passage: " if _is_e5() else ""
    vecs = embedder().encode([prefix + t for t in texts], normalize_embeddings=True,
                             batch_size=16, show_progress_bar=False)
    return [list(map(float, v)) for v in vecs]


def embed_query(text: str) -> list[float]:
    prefix = "query: " if _is_e5() else ""
    v = embedder().encode([prefix + text], normalize_embeddings=True, show_progress_bar=False)[0]
    return list(map(float, v))


# --- Writes -----------------------------------------------------------------------
def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Chroma metadata must be str/int/float/bool and never None."""
    out = {}
    for k, v in meta.items():
        if v is None:
            v = ""
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def existing_chunk_hashes(collection_name: str, university_id: str,
                          hashes: Iterable[str]) -> set[str]:
    hashes = list(dict.fromkeys(hashes))
    if not hashes:
        return set()
    col = collection(collection_name)
    found: set[str] = set()
    for i in range(0, len(hashes), 200):
        res = col.get(where={"$and": [{"university_id": university_id},
                                      {"chunk_hash": {"$in": hashes[i:i + 200]}}]},
                      include=["metadatas"])
        found.update(m["chunk_hash"] for m in res["metadatas"] or [])
    return found


def add_chunks(collection_name: str, doc_id: int, texts: list[str], embed_texts: list[str],
               metadatas: list[dict[str, Any]]) -> int:
    if not texts:
        return 0
    with _lock:
        col = collection(collection_name)
        vecs = embed_passages(embed_texts)
        ids = [f"doc{doc_id}-c{m['chunk_index']}" for m in metadatas]
        col.upsert(ids=ids, documents=texts, embeddings=vecs,
                   metadatas=[_clean_meta(m) for m in metadatas])
        _bump()
        return len(ids)


def delete_document(collection_name: str, doc_id: int) -> None:
    with _lock:
        if collection_name in existing_collections():
            collection(collection_name).delete(where={"doc_id": doc_id})
            _bump()


def update_document_metadata(collection_name: str, doc_id: int, fields: dict[str, Any]) -> None:
    with _lock:
        col = collection(collection_name)
        res = col.get(where={"doc_id": doc_id}, include=["metadatas"])
        if not res["ids"]:
            return
        metas = [{**m, **_clean_meta(fields)} for m in res["metadatas"]]
        col.update(ids=res["ids"], metadatas=metas)
        _bump()


def count(collection_name: str, where: dict | None = None) -> int:
    if collection_name not in existing_collections():
        return 0
    col = collection(collection_name)
    if where:
        return len(col.get(where=where, include=[])["ids"])
    return col.count()


# --- Reads ------------------------------------------------------------------------
def vector_search(collection_names: list[str], university_id: str, query: str,
                  k: int) -> list[dict[str, Any]]:
    qv = embed_query(query)
    hits: list[dict[str, Any]] = []
    present = existing_collections()
    for name in collection_names:
        if name not in present:
            continue
        col = collection(name)
        n = col.count()
        if n == 0:
            continue
        res = col.query(query_embeddings=[qv], n_results=min(k, n),
                        where={"university_id": university_id},
                        include=["documents", "metadatas", "distances"])
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                        res["metadatas"][0], res["distances"][0]):
            hits.append({"id": cid, "text": doc, "meta": meta, "collection": name,
                         "similarity": 1.0 - float(dist)})
    hits.sort(key=lambda h: h["similarity"], reverse=True)
    return hits


def all_chunks(collection_names: list[str], university_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    present = existing_collections()
    for name in collection_names:
        if name not in present:
            continue
        res = collection(name).get(where={"university_id": university_id},
                                   include=["documents", "metadatas"])
        for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
            out.append({"id": cid, "text": doc, "meta": meta, "collection": name})
    return out
