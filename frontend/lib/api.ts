// Typed client for the UniGuide AI FastAPI backend (proxied at /api by next.config.mjs).

export type University = {
  id: string;
  name_ar: string;
  name_en: string;
  allowed_domains: string[];
  indexed_documents: number;
};

export type Citation = {
  n: number;
  university: string;
  university_id: string;
  collection: string;
  source_type: string;
  title: string;
  url: string;
  link: string;
  file_ref: string;
  page: number | null;
  section: string;
  article: string;
  academic_year: string;
  version: string;
  retrieved_at: string;
  passage: string;
  kind: string;
  doc_id: number;
};

export type ChatResponse = {
  status: "answered" | "conflict" | "no_answer" | "needs_university" | "error";
  answer: string;
  citations: Citation[];
  university?: { id: string; name: string } | null;
  universities?: { id: string; name: string }[];
  mode?: string;
  model?: string;
  multi_source?: boolean;
  notice?: string;
};

export type SourceDoc = {
  id: number;
  university_id: string;
  collection: string;
  source_type: string;
  title: string;
  url: string | null;
  file_ref: string | null;
  origin: string;
  parent_url: string | null;
  status: string;
  error: string | null;
  version: string;
  academic_year: string;
  chunk_count: number;
  page_count: number;
  retrieved_at: string | null;
  updated_at: string;
  has_file: boolean;
};

export type Stats = {
  groups: { university_id: string; collection: string; source_type: string; status: string; n: number; chunks: number; last: string }[];
  programs: { university_id: string; n: number }[];
  courses: { university_id: string; n: number }[];
  collections: Record<string, number>;
  hackathon_dataset_present: boolean;
  ingestion_running: boolean;
  last_run: { id: number; kind: string; status: string; started_at: string; finished_at: string | null; summary: Record<string, unknown> } | null;
};

export type Program = { id: number; name: string; faculty: string; campus: string; degree: string; min_average: string; streams: string; academic_year: string; source_url: string; retrieved_at: string };
export type Course = { code: string; title: string; credits: string; category: string; semester: string; page: number; requisites_raw: string; program: string; document_title: string; doc_id: number };
export type IngestError = { id: number; run_id: number | null; document_id: number | null; url: string | null; stage: string; message: string; created_at: string };
export type Run = { id: number; university_id: string | null; kind: string; status: string; started_at: string; finished_at: string | null; summary: Record<string, number | string> };

async function request<T>(path: string, init?: RequestInit & { admin?: string }): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.admin) headers.set("X-Admin-Token", init.admin);
  if (init?.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ llm: string | null; mode: string; embedding_model: string; collections: Record<string, number>; ingestion_running: boolean }>("/api/health"),
  universities: () => request<University[]>("/api/universities"),
  chat: (question: string, university_id: string | null) =>
    request<ChatResponse>("/api/chat", { method: "POST", body: JSON.stringify({ question, university_id }) }),
  sources: () => request<SourceDoc[]>("/api/sources"),
  stats: () => request<Stats>("/api/sources/stats"),
  programs: (uid: string) => request<Program[]>(`/api/programs?university_id=${encodeURIComponent(uid)}`),
  courses: (uid: string, q = "") => request<Course[]>(`/api/courses?university_id=${encodeURIComponent(uid)}&q=${encodeURIComponent(q)}`),
  errors: () => request<IngestError[]>("/api/errors?limit=200"),
  runs: () => request<Run[]>("/api/ingest/runs"),
  startWebsite: (admin: string, university_id: string, discover: boolean, max_pages?: number, max_pdfs?: number) =>
    request<{ started: boolean }>("/api/ingest/website", { method: "POST", admin, body: JSON.stringify({ university_id, discover, max_pages, max_pdfs }) }),
  addUrl: (admin: string, university_id: string, url: string) =>
    request<{ started: boolean }>("/api/sources/url", { method: "POST", admin, body: JSON.stringify({ university_id, url }) }),
  refresh: (admin: string, ids: number[]) =>
    request<{ started: boolean }>("/api/sources/refresh", { method: "POST", admin, body: JSON.stringify({ ids }) }),
  remove: (admin: string, id: number) => request<{ deleted: number }>(`/api/sources/${id}`, { method: "DELETE", admin }),
  upload: (admin: string, form: FormData) =>
    request<{ id: number; status: string; chunks?: number; courses?: number; error?: string; warnings?: string[] }>("/api/sources/upload", { method: "POST", admin, body: form }),
};

const AR_DATE = new Intl.DateTimeFormat("ar-PS-u-nu-latn", { year: "numeric", month: "long", day: "numeric" });
const AR_DATETIME = new Intl.DateTimeFormat("ar-PS-u-nu-latn", { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

export function arDate(iso?: string | null, withTime = false): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return (withTime ? AR_DATETIME : AR_DATE).format(d);
}

export const STATUS_AR: Record<string, string> = {
  indexed: "مفهرس",
  failed: "فشل",
  duplicate: "مكرر",
  pending: "قيد الانتظار",
  skipped: "متجاهل",
  running: "قيد التنفيذ",
  finished: "مكتمل",
};

export const TYPE_AR: Record<string, string> = {
  website: "صفحة ويب",
  pdf: "PDF",
  dataset: "بيانات منظمة",
  text: "نص",
  pending: "—",
};

export const COLLECTION_AR: Record<string, string> = {
  hackathon_official_documents: "وثائق الهاكاثون الرسمية",
  alquds_official_website: "موقع جامعة القدس الرسمي",
  alquds_uploaded_academic_documents: "وثائق جامعة القدس المرفوعة",
};
