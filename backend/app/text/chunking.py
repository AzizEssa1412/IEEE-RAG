"""Structure-aware chunking.

Extractors produce a list of Blocks (paragraph / table-row / heading) carrying page and
section information. Chunks never cross a page or section boundary, table rows are never
split, and each chunk records the article number ("المادة ...") it belongs to.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ..config import settings
from .arabic import find_article_number, normalize_for_search


@dataclass
class Block:
    text: str
    kind: str = "para"          # para | heading | table
    page: int = 0               # 1-based for PDFs, 0 for web pages
    section: str = ""


@dataclass
class Chunk:
    text: str
    page: int
    section: str
    article: str
    index: int = 0
    hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = content_hash(self.text)


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", normalize_for_search(text)).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


_SENTENCE_SPLIT = re.compile(r"(?<=[\.\!\?؟،؛:])\s+|\n")


def _split_long(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, cur = [], ""
    for piece in _SENTENCE_SPLIT.split(text):
        if not piece:
            continue
        if len(cur) + len(piece) + 1 > size and cur:
            parts.append(cur.strip())
            cur = ""
        while len(piece) > size:  # pathological: no punctuation at all
            cut = piece.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            parts.append(piece[:cut].strip())
            piece = piece[cut:]
        cur += piece + " "
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _tail(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    tail = text[-n:]
    sp = tail.find(" ")
    return tail[sp + 1:] if 0 <= sp < n // 2 else tail


def chunk_blocks(blocks: list[Block], size: int | None = None,
                 overlap: int | None = None) -> list[Chunk]:
    size = size or settings.chunk_chars
    overlap = settings.chunk_overlap if overlap is None else overlap
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    fresh = 0                     # characters added since the last flush (excludes overlap)
    cur_key: tuple[int, str] | None = None
    current_article = ""
    chunk_article = ""

    def flush(carry: bool) -> None:
        nonlocal buf, buf_len, chunk_article, fresh
        text = "\n".join(buf).strip()
        if text and cur_key is not None and fresh > 0:
            art = chunk_article or find_article_number(text)
            chunks.append(Chunk(text=text, page=cur_key[0], section=cur_key[1], article=art))
        if carry and text and overlap > 0:
            t = _tail(text, overlap)
            buf, buf_len = [t], len(t)
        else:
            buf, buf_len = [], 0
        fresh = 0
        chunk_article = current_article

    for b in blocks:
        text = b.text.strip()
        if not text:
            continue
        key = (b.page, b.section)
        if key != cur_key:
            if buf:
                flush(carry=False)
            cur_key = key
            chunk_article = current_article
        art = find_article_number(text[:60])
        if art and art != current_article:
            if fresh and fresh > size // 3:
                flush(carry=False)  # start a new chunk at each new article
            current_article = art
            if not fresh:
                chunk_article = art
        pieces = [text] if b.kind == "table" else _split_long(text, size)
        for piece in pieces:
            if buf_len + len(piece) + 1 > size and fresh:
                flush(carry=b.kind != "table")
            buf.append(piece)
            buf_len += len(piece) + 1
            fresh += len(piece)
    if buf:
        flush(carry=False)

    for i, c in enumerate(chunks):
        c.index = i
    return chunks
