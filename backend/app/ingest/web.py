"""Polite website fetching and main-content extraction for official university pages.

Pipeline stage:  URL -> robots.txt check -> rate-limited fetch -> main-content extraction
(menus/footers/news grids removed, Arabic + tables preserved) -> Blocks + metadata.

Hard rules implemented here:
  * robots.txt is honoured (including Crawl-delay); unreachable robots.txt => do not crawl
  * only allow-listed university domains; no portals, logins, admin or search pages
  * HTTP 401/403 and CAPTCHA/challenge pages are recorded as failures, never bypassed
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from ..config import settings
from ..text.arabic import clean_text, find_academic_year, normalize_for_search
from ..text.chunking import Block

# --------------------------------------------------------------------------------------
# URL policy
# --------------------------------------------------------------------------------------
_PRIVATE_PATTERNS = re.compile(
    r"(wp-admin|wp-login|/login|signin|sign-in|/portal|my-account|ritaj|student-portal|"
    r"/feed/?$|[?&]s=|/wp-json|/xmlrpc|/cart|/checkout|card-payment)",
    re.IGNORECASE,
)


def host_allowed(url: str, allowed_domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {d.lower() for d in allowed_domains}


def is_private_or_unsafe(url: str) -> bool:
    p = urlparse(url)
    return p.scheme not in ("http", "https") or bool(_PRIVATE_PATTERNS.search(unquote(url)))


def canonical_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    path = p.path or "/"
    return f"{p.scheme}://{(p.hostname or '').lower()}{path}" + (f"?{p.query}" if p.query else "")


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


# Relevance (for discovered links only; admin-approved URLs are always ingested).
_ACADEMIC_TERMS = [
    "تسجيل", "قبول", "التحاق", "انظمه", "نظام", "تعليمات", "لوايح", "لايحه", "خطه", "خطط",
    "مساق", "متطلب", "ساعات", "تخرج", "تقويم", "اكاديمي", "سحب", "انسحاب", "حضور", "غياب",
    "امتحان", "علامات", "دليل", "برامج", "برنامج", "تخصص", "بكالوريوس", "دبلوم", "تجسير",
    "رسوم", "شهادات", "مصدقات", "وثايق", "نماذج", "بجروت", "ماجستير", "دكتوراه", "صيفي",
    "registration", "admission", "regulation", "calendar", "calender", "plan", "program",
    "guide", "bachelor", "diploma", "instruction", "rules", "exam", "graduat", "academic",
    "fees", "bagrut", "certificates", "document-issuance", "forms", "bulletin", "masters",
    "phd", "study",
]
_IRRELEVANT_TERMS = [
    "our-team", "team", "thanks", "university-days", "museum", "sport", "voluntary",
    "gallery", "video", "event", "/tag/", "/author/", "/category/", "empowerment",
    "activities", "social-survey", "exchange", "/programs/",
]


# News/announcement posts (URLs with a numeric post id) are only relevant when they are
# about admission, registration, withdrawal, calendars or graduation — not general news.
_NEWS_POST = re.compile(r"/(news|announc)[^/]*/.*\d{3,}/|/\d{4,}/[^/]+/?$")
_NEWS_STRONG_TERMS = ["تسجيل", "قبول", "التحاق", "انسحاب", "سحب", "تقويم", "تخرج", "امتحان",
                      "registration", "admission", "calendar"]


def relevance_score(url: str, anchor: str = "") -> int:
    decoded = unquote(url)
    hay = normalize_for_search(decoded + " " + anchor)
    if any(t in hay for t in _IRRELEVANT_TERMS):
        return 0
    if _NEWS_POST.search(decoded) and not any(t in hay for t in _NEWS_STRONG_TERMS):
        return 0
    return sum(1 for t in _ACADEMIC_TERMS if t in hay)


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------
@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int = 0
    final_url: str = ""
    content_type: str = ""
    content: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str = ""
    stage: str = "fetch"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class PoliteFetcher:
    """httpx client that obeys robots.txt and a per-host minimum delay."""

    def __init__(self, user_agent: str | None = None, delay: float | None = None,
                 timeout: float | None = None, transport: httpx.BaseTransport | None = None):
        self.user_agent = user_agent or settings.user_agent
        self.delay = settings.crawl_delay_seconds if delay is None else delay
        self.client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Language": "ar,en;q=0.5"},
            timeout=timeout or settings.request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    # -- robots ------------------------------------------------------------------------
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base in self._robots:
            return self._robots[base]
        rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        try:
            self._wait(p.netloc)
            r = self.client.get(base + "/robots.txt", follow_redirects=True)
            if r.status_code >= 500:
                rp = None  # server error: be conservative and do not crawl
            elif r.status_code >= 400:
                rp.parse([])  # no robots.txt => everything allowed (RFC 9309)
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            rp = None
        self._robots[base] = rp
        return rp

    def robots_allows(self, url: str) -> tuple[bool, str]:
        rp = self._robots_for(url)
        if rp is None:
            return False, "تعذّر قراءة ملف robots.txt؛ لم يتم الزحف احتياطاً (robots.txt unreachable)"
        if not rp.can_fetch(self.user_agent, url):
            return False, "الصفحة ممنوعة في robots.txt (disallowed by robots.txt)"
        return True, ""

    def _crawl_delay(self, netloc: str) -> float:
        rp = self._robots.get(f"https://{netloc}") or self._robots.get(f"http://{netloc}")
        cd = None
        if rp is not None:
            try:
                cd = rp.crawl_delay(self.user_agent)
            except Exception:  # pragma: no cover
                cd = None
        return max(self.delay, float(cd or 0))

    def _wait(self, netloc: str) -> None:
        with self._lock:
            wait = self._last.get(netloc, 0) + self._crawl_delay(netloc) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[netloc] = time.monotonic()

    # -- fetch ------------------------------------------------------------------------
    def fetch(self, url: str, allowed_domains: list[str], max_bytes: int | None = None,
              max_redirects: int = 5) -> FetchResult:
        current = url
        for _ in range(max_redirects + 1):
            if is_private_or_unsafe(current):
                return FetchResult(url, False, error="رابط خاص أو بوابة تسجيل دخول — تم تجاهله "
                                   "(private/login URL skipped)", stage="policy")
            if not host_allowed(current, allowed_domains):
                return FetchResult(url, False, error="النطاق غير معتمد لهذه الجامعة: "
                                   f"{urlparse(current).hostname}", stage="policy")
            ok, why = self.robots_allows(current)
            if not ok:
                return FetchResult(url, False, error=why, stage="robots")
            resp = self._get_with_retries(current, max_bytes)
            if isinstance(resp, FetchResult):
                resp.url = url
                return resp
            if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                current = urljoin(current, resp.headers["location"])
                continue
            return self._to_result(url, current, resp)
        return FetchResult(url, False, error="عدد كبير من التحويلات (too many redirects)")

    def _get_with_retries(self, url: str, max_bytes: int | None):
        netloc = urlparse(url).netloc
        last_err = ""
        for attempt in range(3):
            self._wait(netloc)
            try:
                with self.client.stream("GET", url) as r:
                    if r.status_code in (429, 503) and attempt < 2:
                        ra = r.headers.get("retry-after", "")
                        time.sleep(min(float(ra) if ra.isdigit() else 5.0 * (attempt + 1), 60))
                        continue
                    chunks, size = [], 0
                    for part in r.iter_bytes():
                        size += len(part)
                        if max_bytes and size > max_bytes:
                            return FetchResult(url, False, status=r.status_code,
                                               error=f"الملف أكبر من الحد المسموح ({max_bytes} بايت)")
                        chunks.append(part)
                    body = b"".join(chunks)
                    return httpx.Response(r.status_code, headers=r.headers, content=body,
                                          request=r.request)
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(2.0 * (attempt + 1))
        return FetchResult(url, False, error=f"فشل الاتصال (network error): {last_err}")

    @staticmethod
    def _to_result(url: str, final: str, r: httpx.Response) -> FetchResult:
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        res = FetchResult(url=url, ok=False, status=r.status_code, final_url=final,
                          content_type=ctype, content=r.content, headers=dict(r.headers))
        if r.status_code in (401, 403):
            res.error = f"الوصول مقيّد (HTTP {r.status_code}) — لم تتم محاولة تجاوز الحماية"
            res.stage = "access"
        elif r.status_code >= 400:
            res.error = f"HTTP {r.status_code}"
        elif ctype.startswith("text/html") and looks_like_challenge(res.text):
            res.error = "صفحة تحقق CAPTCHA/حماية — تم التجاهل دون تجاوز"
            res.stage = "access"
        else:
            res.ok = True
        return res


def looks_like_challenge(html: str) -> bool:
    low = html[:20000].lower()
    markers = ("g-recaptcha", "h-captcha", "cf-challenge", "challenge-platform",
               "attention required! | cloudflare", "verify you are human")
    return any(m in low for m in markers) and "<main" not in low


# --------------------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------------------
@dataclass
class PageContent:
    title: str
    blocks: list[Block]
    pdf_links: list[tuple[str, str]]
    links: list[tuple[str, str]]
    date_modified: str = ""
    date_published: str = ""
    academic_year: str = ""
    method: str = "main-dom"
    data_tables: list[dict] = field(default_factory=list)  # structured tables from inline data

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


_NOISE_TAGS = ["script", "style", "noscript", "template", "form", "input", "select", "option",
               "button", "iframe", "svg", "canvas", "video", "audio", "nav", "header", "footer",
               "aside", "picture", "img"]
# NOTE: Impreza "w-popup" boxes are NOT noise on this site: the registration page keeps
# its actual guidance (objectives, student responsibilities, forms) inside popups.
_NOISE_CLASS = re.compile(
    r"^(w-grid|w-grid-list|footer|footer-about|breadcrumb|w-socials|w-search|w-nav|menu|"
    r"sharing|sidebar|cookie|w-separator|pagination|related|wpspw-post-wrap|wpspw-slick|"
    r"wpforms-container|w-counter)$",
    re.IGNORECASE,
)
_JUNK_EXACT = {"row backgrounds", "المزيد", "اقرأ المزيد", "read more", "× close panel",
               "close panel", "إعلانات إعلانات القبول"}
_CSS_LIKE = re.compile(r"^[.#][\w-]+(\s*[{,].*)?$")
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {"p", "div", "section", "article", "ul", "ol", "li", "dl", "dt", "dd",
               "blockquote", "pre", "figure", "figcaption", "address", "details", "summary",
               "tbody", "thead", "tr", "main", "center"}
_JUNK_LINE = re.compile(r"^[\W\d_]{0,3}$", re.UNICODE)
_TITLE_SUFFIX = re.compile(r"\s*[-|–]\s*Al-Quds University\s*$", re.IGNORECASE)
_DATA_ARRAY = re.compile(r"var\s+(\w+)\s*=\s*(\[\s*\[.*?\]\s*\])\s*;", re.S)


def _json_ld_dates(soup: BeautifulSoup) -> tuple[str, str]:
    modified = published = ""
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in nodes if isinstance(nodes, list) else []:
            if isinstance(n, dict) and n.get("@type") in ("WebPage", "Article", "NewsArticle"):
                modified = modified or n.get("dateModified", "")
                published = published or n.get("datePublished", "")
    if not modified:
        m = soup.find("meta", attrs={"property": "article:modified_time"})
        modified = m.get("content", "") if m else ""
    return modified, published


def _table_rows(table: Tag) -> tuple[list[str], list[list[str]]]:
    headers: list[str] = []
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        vals = [clean_text(c.get_text(" ", strip=True)) for c in cells]
        if not any(vals):
            continue
        if not headers and (tr.find("th") is not None or tr.find_parent("thead") is not None):
            headers = vals
            continue
        rows.append(vals)
    return headers, rows


def format_row(headers: list[str], row: list[str]) -> str:
    """Make each table row self-describing ("column: value | ...") so it survives chunking."""
    if headers and len(headers) == len(row):
        return " | ".join(f"{h}: {v}" for h, v in zip(headers, row) if v)
    return " | ".join(v for v in row if v)


def _inline_data_tables(soup: BeautifulSoup, empty_headers: list[list[str]]) -> list[dict]:
    """Structured data that the page renders client-side (e.g. `var programsData=[[...]]`)."""
    tables = []
    for s in soup.find_all("script"):
        src = s.string or ""
        for name, arr in _DATA_ARRAY.findall(src):
            try:
                rows = json.loads(arr)
            except json.JSONDecodeError:
                continue
            if not rows or not all(
                isinstance(r, list) and all(isinstance(c, (str, int, float)) for c in r)
                for r in rows
            ):
                continue
            width = len(rows[0])
            headers = next((h for h in empty_headers if len(h) == width), [])
            tables.append({"name": name, "headers": headers,
                           "rows": [[clean_text(str(c)) for c in r] for r in rows]})
    return tables


def extract_page(html: str, url: str) -> PageContent:
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("title")
    title = _TITLE_SUFFIX.sub("", clean_text(title_tag.get_text())) if title_tag else ""
    modified, published = _json_ld_dates(soup)

    main = soup.find("main") or soup.find(id="page-content") or soup.find("article") or soup.body
    links: list[tuple[str, str]] = []
    pdfs: list[tuple[str, str]] = []
    if main is not None:
        for a in main.find_all("a", href=True):
            href = canonical_url(urljoin(url, a["href"]))
            anchor = clean_text(a.get_text(" ", strip=True))
            (pdfs if is_pdf_url(href) else links).append((href, anchor))

    # Empty table shells give us the column names of client-side rendered data tables.
    empty_headers = []
    for t in (main.find_all("table") if main is not None else []):
        h, r = _table_rows(t)
        if h and not r:
            empty_headers.append(h)
    data_tables = _inline_data_tables(soup, empty_headers)

    blocks: list[Block] = []
    if main is not None:
        for t in main.find_all(_NOISE_TAGS):
            t.decompose()
        for t in main.find_all(class_=_NOISE_CLASS):
            t.decompose()
        blocks = _walk_blocks(main)

    for dt in data_tables:
        section = "جدول بيانات الصفحة"
        if dt["headers"]:
            blocks.append(Block(" | ".join(dt["headers"]), "heading", 0, section))
        for row in dt["rows"]:
            blocks.append(Block(format_row(dt["headers"], row), "table", 0, section))

    blocks = _dedupe_blocks(blocks)
    method = "main-dom"
    if not blocks:
        # Only when the page has no usable <main> content do we fall back to Trafilatura,
        # and we keep only paragraph-length lines so menus cannot leak in as "knowledge".
        fallback = _trafilatura_blocks(html, url)
        if sum(len(b.text) for b in fallback) >= 200:
            blocks, method = fallback, "trafilatura"

    full = title + "\n" + "\n".join(b.text for b in blocks[:40])
    return PageContent(title=title, blocks=blocks, pdf_links=list(dict.fromkeys(pdfs)),
                       links=list(dict.fromkeys(links)), date_modified=modified,
                       date_published=published, academic_year=find_academic_year(full),
                       method=method, data_tables=data_tables)


def is_junk(text: str) -> bool:
    t = text.strip()
    return (not t or bool(_JUNK_LINE.match(t)) or t.lower() in _JUNK_EXACT
            or bool(_CSS_LIKE.match(t)) or t.lower().startswith("html "))


def _walk_blocks(root: Tag) -> list[Block]:
    blocks: list[Block] = []
    section = ""
    inline: list[str] = []

    def flush() -> None:
        text = clean_text(" ".join(inline))
        inline.clear()
        if text and not is_junk(text):
            blocks.append(Block(text, "para", 0, section))

    def walk(node: Tag) -> None:
        nonlocal section
        for child in node.children:
            if isinstance(child, NavigableString):
                s = str(child).strip()
                if s:
                    inline.append(s)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in _HEADINGS:
                flush()
                text = clean_text(child.get_text(" ", strip=True))
                if text and not is_junk(text):
                    section = text[:200]
                    blocks.append(Block(text, "heading", 0, section))
            elif name == "table":
                flush()
                headers, rows = _table_rows(child)
                if headers and rows:
                    blocks.append(Block(" | ".join(headers), "heading", 0, section))
                for row in rows:
                    blocks.append(Block(format_row(headers, row), "table", 0, section))
            elif name == "br":
                flush()
            elif name in _BLOCK_TAGS:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    walk(root)
    flush()
    return blocks


def _dedupe_blocks(blocks: list[Block]) -> list[Block]:
    seen: set[str] = set()
    out: list[Block] = []
    for b in blocks:
        key = normalize_for_search(b.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    while out and out[-1].kind == "heading":  # trailing heading carries no knowledge
        out.pop()
    return out


def _trafilatura_blocks(html: str, url: str) -> list[Block]:
    import trafilatura

    text = trafilatura.extract(html, url=url, include_tables=True, include_comments=False,
                               favor_recall=True, output_format="txt") or ""
    lines = [clean_text(p) for p in text.split("\n")]
    return [Block(p, "para", 0, "") for p in lines if len(p) >= 40 and not is_junk(p)]
