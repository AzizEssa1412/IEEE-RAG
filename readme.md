# UniGuide AI — Arabic academic assistant (IEEE RAG Hackathon)

UniGuide AI answers students' academic questions **only from official university documents**,
in Arabic (including Palestinian dialect), with clickable citations to the exact web page or
PDF page. It never answers from model memory: if the indexed sources don't contain the answer,
it says so.

Two knowledge sources are supported and kept strictly separate:

| ChromaDB collection | Contents |
|---|---|
| `hackathon_official_documents` | The official IEEE Hackathon academic dataset (upload via the dashboard) |
| `alquds_official_website` | Public academic pages + linked PDFs crawled from https://www.alquds.edu/ar/ |
| `alquds_uploaded_academic_documents` | Al-Quds academic files uploaded by an admin (e.g. study plans) |

Every chunk also carries `university_id`, and every search filters on it. Questions are routed to
exactly **one** university before retrieval. Rules of different universities are never mixed.

## Stack (all free / open source)

- **Backend:** FastAPI, SQLite (registry), ChromaDB (vectors), sentence-transformers
  `intfloat/multilingual-e5-small` (local embeddings), httpx + BeautifulSoup + Trafilatura
  (web), PyMuPDF (PDF), openpyxl (XLSX)
- **LLM (optional):** Gemini API free tier (`GEMINI_API_KEY`) or local Ollama. With neither,
  the assistant runs in **extractive mode** and quotes the official passages verbatim.
- **Frontend:** Next.js 15, Arabic RTL, IBM Plex Sans Arabic

## Run it

```bash
# Backend
cd backend
python -m venv .venv && .venv/Scripts/activate      # Windows (use bin/activate on Linux/macOS)
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
set ADMIN_TOKEN=choose-a-secret                       # export on Linux/macOS
set GEMINI_API_KEY=...                                # optional (free tier)
uvicorn app.main:app --port 8000

# Frontend (second terminal)
cd frontend
npm install
npm run dev        # http://localhost:3000  (proxies /api to :8000)
```

Optional local LLM: install Ollama, run `ollama pull qwen2.5:7b`, and set `LLM_PROVIDER=ollama`.

### Ingest data

From the dashboard **«مصادر المعلومات الأكاديمية»** (`/sources`): enter the admin token, then
use «بدء فهرسة الموقع», «إضافة رابط جامعي معتمد», «رفع وثيقة أكاديمية», refresh, or delete.

Or from the CLI (same code paths):

```bash
cd backend
python -m scripts.ingest website --university alquds --max-pages 45 --max-pdfs 25
python -m scripts.ingest upload ../data/uploads/AcademicPlan-AQU-Eng-CE2010.pdf --university alquds --program "B.Sc. Computer Engineering"
python -m scripts.ingest upload path/to/hackathon_dataset.pdf --university alquds --hackathon
python -m scripts.ingest report     # status of every source, including failures
python -m scripts.demo              # the 7 demonstration scenarios against the real index
```

### Tests

```bash
cd backend
python -m pytest -q        # fully offline: temp DB, mock HTTP, deterministic embedder, synthetic data
```

## How ingestion works

`URL → robots.txt check → rate-limited fetch → main-content extraction → Arabic cleaning →
metadata → structure-aware chunking → embeddings → ChromaDB`, with every outcome in SQLite.

- **Compliance:** robots.txt is obeyed (an unreachable robots.txt means no crawling), at least
  1.5 s between requests (or the site's `Crawl-delay`), retries respect `Retry-After`. Only
  allow-listed domains are fetched. Login, portal, payment, admin and search URLs are refused.
  HTTP 401/403 and CAPTCHA pages are recorded as failures and are never bypassed.
- **Content, not chrome:** only `<main>` is used. Menus, headers, footers, news sliders and
  forms are removed. Blocks that repeat on ≥30% of crawled pages (site templates) are detected and
  excluded. Trafilatura is used only as a fallback for pages without a usable `<main>`, and only
  paragraph-length lines are kept from it.
- **Tables:** HTML and PDF tables become self-describing rows (`column: value | …`) that are never
  split across chunks. Client-side data tables embedded in the page (e.g. the 2026/2027 bachelor
  program guide's `programsData`) are extracted with their real column headers.
- **PDFs** linked from allowed pages are downloaded (size-limited), parsed page by page, and kept
  so citations open the exact page. Scanned PDFs without a text layer are reported, not guessed.
- **Dedup:** identical documents (same normalized content) are marked `duplicate`; identical
  chunks are not stored twice.
- **Refresh:** unchanged pages just get a new retrieval date; changed pages are re-indexed.

Each chunk stores: university, source type, title, URL or file reference, page, section heading,
article number (`المادة …`), academic year, document version (page `dateModified` from the site's
JSON-LD, or the date printed on the PDF), retrieval date, and the full passage.

### Structured course data

PDF tables and CSV/XLSX/JSON files are scanned for course code, title, credit hours (theory/lab),
prerequisites/corequisites (`P-…`, `C-…`, "See Note N" resolved to the note text), category
(e.g. `Table III-a`), and recommended semester (from the study-plan grid). Requisites pointing to
course numbers that don't exist in the same document are kept as written and flagged as
unresolved. Nothing is invented or "fixed".

## How answering works

1. **Route:** detect the university (aliases, plus Arabic proclitics like «بجامعة»). Unknown
   universities and confusable names (e.g. **جامعة القدس المفتوحة** is a different institution)
   get an honest refusal. With no university mentioned or selected, the assistant asks.
2. **Retrieve:** dialect-aware query expansion (e.g. «بقدر أنزل» → «أستطيع تسجيل»), dense e5 search
   plus BM25 over normalized Arabic, fused with reciprocal rank fusion. A course code in the question
   also pulls that course's structured record with its source page.
3. **Evidence gate:** below the similarity threshold → «لم أجد إجابة… في المصادر الرسمية».
4. **Answer:** the LLM receives only numbered passages and must cite `[n]`, answer `NO_ANSWER` when
   the passages don't cover the question, and flag conflicting sources with their dates instead of
   resolving them. Citations shown to the student are built from stored metadata, never from model
   text.

## Troubleshooting

- **Windows Smart App Control** may block a native DLL on the first import
  (`DLL load failed … Application Control policy`). Re-running usually succeeds once the file's
  reputation check completes. Otherwise, run from a folder outside OneDrive or allow Python.
- First start downloads the embedding model (~470 MB) from Hugging Face.
