"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  arDate,
  COLLECTION_AR,
  STATUS_AR,
  TYPE_AR,
  type Course,
  type IngestError,
  type Program,
  type Run,
  type SourceDoc,
  type Stats,
  type University,
} from "@/lib/api";

type Tab = "web" | "pdf" | "uploads" | "programs" | "courses" | "errors" | "runs";
const TABS: { id: Tab; label: string }[] = [
  { id: "web", label: "صفحات الويب الرسمية" },
  { id: "pdf", label: "ملفات PDF من الموقع" },
  { id: "uploads", label: "الوثائق المرفوعة" },
  { id: "programs", label: "البرامج الأكاديمية" },
  { id: "courses", label: "المساقات المستخرجة" },
  { id: "errors", label: "أخطاء الفهرسة" },
  { id: "runs", label: "سجل العمليات" },
];

function statusBadge(s: string) {
  const cls = s === "indexed" || s === "finished" ? "ok" : s === "failed" ? "bad" : s === "duplicate" ? "warn" : "gray";
  return <span className={`badge ${cls}`}>{STATUS_AR[s] ?? s}</span>;
}

function readToken(): string {
  try {
    return sessionStorage.getItem("uniguide.admin") || "";
  } catch {
    return "";
  }
}

function safeDecode(u: string): string {
  try {
    return decodeURI(u);
  } catch {
    return u;
  }
}

export default function SourcesPage() {
  const [token, setToken] = useState("");
  const [tokenInput, setTokenInput] = useState("");
  const [universities, setUniversities] = useState<University[]>([]);
  const [uni, setUni] = useState("alquds");
  const [stats, setStats] = useState<Stats | null>(null);
  const [docs, setDocs] = useState<SourceDoc[]>([]);
  const [programs, setPrograms] = useState<Program[]>([]);
  const [courses, setCourses] = useState<Course[]>([]);
  const [courseQ, setCourseQ] = useState("");
  const [errors, setErrors] = useState<IngestError[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [tab, setTab] = useState<Tab>("web");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [msg, setMsg] = useState<{ kind: "ok" | "bad" | "warn"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const [url, setUrl] = useState("");
  const [discover, setDiscover] = useState(true);
  const [maxPages, setMaxPages] = useState(45);
  const [maxPdfs, setMaxPdfs] = useState(25);
  const [file, setFile] = useState<File | null>(null);
  const [target, setTarget] = useState<"uploads" | "hackathon">("uploads");
  const [meta, setMeta] = useState({ title: "", version: "", academic_year: "", program: "" });

  useEffect(() => {
    const t = readToken();
    setToken(t);
    setTokenInput(t);
  }, []);

  const load = useCallback(async () => {
    try {
      const [u, s, d, p, e, r] = await Promise.all([api.universities(), api.stats(), api.sources(), api.programs(uni), api.errors(), api.runs()]);
      setUniversities(u);
      setStats(s);
      setDocs(d);
      setPrograms(p);
      setErrors(e);
      setRuns(r);
    } catch (err) {
      setMsg({ kind: "bad", text: `تعذّر تحميل البيانات من الخادم: ${(err as Error).message}` });
    }
  }, [uni]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api.courses(uni, courseQ).then(setCourses).catch(() => setCourses([]));
  }, [uni, courseQ, stats]);

  useEffect(() => {
    if (!stats?.ingestion_running) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [stats?.ingestion_running, load]);

  const uniObj = universities.find((u) => u.id === uni);
  const uniDocs = useMemo(() => docs.filter((d) => d.university_id === uni), [docs, uni]);
  const webDocs = uniDocs.filter((d) => d.source_type === "website" && d.origin !== "upload");
  const pdfDocs = uniDocs.filter((d) => d.source_type === "pdf" && d.origin !== "upload");
  const uploadDocs = uniDocs.filter((d) => d.origin === "upload");

  const indexedCount = (list: SourceDoc[]) => list.filter((d) => d.status === "indexed").length;
  const passages = uniDocs.reduce((a, d) => a + (d.status === "indexed" ? d.chunk_count : 0), 0);
  const lastUpdated = uniDocs.reduce<string | null>((a, d) => (d.retrieved_at && (!a || d.retrieved_at > a) ? d.retrieved_at : a), null);

  async function guarded(fn: () => Promise<void>) {
    if (!token) {
      setMsg({ kind: "warn", text: "أدخل رمز المسؤول أولاً لتنفيذ هذا الإجراء." });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await fn();
    } catch (e) {
      setMsg({ kind: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
      load();
    }
  }

  function saveToken() {
    setToken(tokenInput);
    try {
      sessionStorage.setItem("uniguide.admin", tokenInput);
    } catch {
      /* storage unavailable */
    }
    setMsg({ kind: "ok", text: "تم حفظ رمز المسؤول لهذه الجلسة." });
  }

  const toggle = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const renderDocs = (list: SourceDoc[], showCollection = false) => {
    if (list.length === 0) return <p className="muted">لا توجد مصادر في هذا القسم بعد.</p>;
    return (
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th aria-label="تحديد" />
              <th>العنوان / الرابط</th>
              {showCollection && <th>المجموعة</th>}
              <th>النوع</th>
              <th>الحالة</th>
              <th>المقاطع القابلة للبحث</th>
              <th>الإصدار / آخر تعديل</th>
              <th>العام الأكاديمي</th>
              <th>آخر تحديث</th>
              <th>إجراء</th>
            </tr>
          </thead>
          <tbody>
            {list.map((d) => (
              <tr key={d.id}>
                <td>
                  <input type="checkbox" checked={selected.has(d.id)} onChange={() => toggle(d.id)} aria-label={`تحديد ${d.title}`} />
                </td>
                <td className="title-cell">
                  <div>{d.title || "—"}</div>
                  {d.url ? (
                    <a href={d.url} target="_blank" rel="noopener noreferrer" className="small ltr">
                      {safeDecode(d.url)}
                    </a>
                  ) : d.has_file ? (
                    <a href={`/api/documents/${d.id}/file`} target="_blank" rel="noopener noreferrer" className="small ltr">
                      {d.file_ref}
                    </a>
                  ) : null}
                  {d.error && (
                    <div className="small" style={{ color: d.status === "failed" ? "var(--bad-ink)" : d.status === "skipped" ? "var(--muted)" : "var(--warn-ink)" }}>
                      {d.error}
                    </div>
                  )}
                </td>
                {showCollection && <td className="small">{COLLECTION_AR[d.collection] ?? d.collection}</td>}
                <td>{TYPE_AR[d.source_type] ?? d.source_type}</td>
                <td>{statusBadge(d.status)}</td>
                <td>{d.chunk_count}</td>
                <td className="small">{d.version ? (/^\d{4}-\d{2}-\d{2}T/.test(d.version) ? arDate(d.version) : d.version) : "—"}</td>
                <td className="small">{d.academic_year || "—"}</td>
                <td className="small">{arDate(d.retrieved_at, true)}</td>
                <td>
                  <button
                    className="btn danger sm"
                    disabled={busy}
                    onClick={() => {
                      if (confirm(`حذف «${d.title}» وجميع المقاطع المفهرسة التابعة له؟`))
                        guarded(async () => {
                          await api.remove(token, d.id);
                          setMsg({ kind: "ok", text: "تم حذف المستند ومقاطعه من قاعدة البحث." });
                        });
                    }}
                  >
                    حذف
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <section className="card">
        <div className="row">
          <div className="grow">
            <h1>مصادر المعلومات الأكاديمية</h1>
            <p className="muted small" style={{ margin: 0 }}>
              جميع إجابات المساعد مأخوذة حصراً من هذه المصادر الرسمية. تُحفظ مصادر كل جامعة في مجموعات منفصلة ولا تُدمج أنظمة جامعتين في إجابة واحدة.
            </p>
          </div>
          <label className="field">
            الجامعة
            <select className="select" value={uni} onChange={(e) => setUni(e.target.value)}>
              {universities.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name_ar}
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>

      {msg && <div className={`banner ${msg.kind}`}>{msg.text}</div>}

      {stats && !stats.hackathon_dataset_present && (
        <div className="banner warn">
          <b>لم يتم إرفاق مجموعة البيانات الأكاديمية الرسمية للهاكاثون بعد.</b> مجموعة «وثائق الهاكاثون الرسمية» فارغة حالياً. يمكنك رفع الوثائق من نموذج «رفع وثيقة
          أكاديمية» أدناه مع اختيار الوجهة «وثائق الهاكاثون الرسمية». ملاحظة: وثيقة تعليمات الهاكاثون نفسها ليست مجموعة بيانات أكاديمية ولا يجب رفعها كمصدر.
        </div>
      )}
      {stats?.ingestion_running && <div className="banner ok">⏳ عملية فهرسة قيد التنفيذ… يتم تحديث الصفحة تلقائياً.</div>}

      <section className="stats">
        <div className="card stat">
          <div className="num">{indexedCount(webDocs)}</div>
          <div className="lbl">صفحة ويب رسمية مفهرسة</div>
        </div>
        <div className="card stat">
          <div className="num">{indexedCount(pdfDocs)}</div>
          <div className="lbl">ملف PDF من الموقع</div>
        </div>
        <div className="card stat">
          <div className="num">{indexedCount(uploadDocs)}</div>
          <div className="lbl">وثيقة مرفوعة</div>
        </div>
        <div className="card stat">
          <div className="num">{programs.length}</div>
          <div className="lbl">برنامج أكاديمي مفهرس</div>
        </div>
        <div className="card stat">
          <div className="num">{passages}</div>
          <div className="lbl">مقطع قابل للبحث</div>
        </div>
        <div className="card stat">
          <div className="num" style={{ color: "var(--bad-ink)" }}>
            {uniDocs.filter((d) => d.status === "failed").length}
          </div>
          <div className="lbl">مصدر تعذّرت فهرسته</div>
        </div>
        <div className="card stat">
          <div className="num" style={{ fontSize: "1rem" }}>
            {arDate(lastUpdated, true)}
          </div>
          <div className="lbl">آخر تحديث</div>
        </div>
      </section>

      <section className="card">
        <h2>رمز المسؤول</h2>
        <div className="row">
          <input className="input grow" type="password" placeholder="أدخل رمز المسؤول (ADMIN_TOKEN)" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} />
          <button className="btn ghost" onClick={saveToken}>
            حفظ
          </button>
          {token ? <span className="badge ok">مفعّل</span> : <span className="badge gray">للعرض فقط</span>}
        </div>
      </section>

      <section className="grid2">
        <div className="card">
          <h2>فهرسة الموقع الرسمي</h2>
          <p className="muted small">
            يبدأ من الصفحات الرسمية المعتمدة ويكتشف صفحات أكاديمية إضافية وملفات PDF مرتبطة بها، مع احترام robots.txt وتأخير بين الطلبات. النطاقات المسموحة:{" "}
            <span className="ltr">{uniObj?.allowed_domains.join(", ")}</span>
          </p>
          <div className="row">
            <label className="row small">
              <input type="checkbox" checked={discover} onChange={(e) => setDiscover(e.target.checked)} /> اكتشاف صفحات إضافية
            </label>
            <label className="field">
              أقصى عدد صفحات
              <input className="input" type="number" min={1} max={200} value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))} style={{ width: 100 }} />
            </label>
            <label className="field">
              أقصى عدد ملفات PDF
              <input className="input" type="number" min={0} max={100} value={maxPdfs} onChange={(e) => setMaxPdfs(Number(e.target.value))} style={{ width: 100 }} />
            </label>
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            <button
              className="btn"
              disabled={busy || stats?.ingestion_running}
              onClick={() =>
                guarded(async () => {
                  await api.startWebsite(token, uni, discover, maxPages, maxPdfs);
                  setMsg({ kind: "ok", text: "بدأت فهرسة الموقع. ستظهر النتائج والأخطاء هنا عند انتهائها." });
                })
              }
            >
              بدء فهرسة الموقع
            </button>
            <button
              className="btn ghost"
              disabled={busy || selected.size === 0 || stats?.ingestion_running}
              onClick={() =>
                guarded(async () => {
                  await api.refresh(token, [...selected]);
                  setMsg({ kind: "ok", text: `بدأ تحديث ${selected.size} مصدر محدد.` });
                  setSelected(new Set());
                })
              }
            >
              تحديث المصادر المحددة ({selected.size})
            </button>
          </div>
          <hr style={{ border: 0, borderTop: "1px solid var(--line)", margin: "14px 0" }} />
          <h2>إضافة رابط جامعي معتمد</h2>
          <div className="row">
            <input className="input grow" style={{ direction: "ltr" }} placeholder="https://www.alquds.edu/ar/..." value={url} onChange={(e) => setUrl(e.target.value)} />
            <button
              className="btn"
              disabled={busy || !url.trim() || stats?.ingestion_running}
              onClick={() =>
                guarded(async () => {
                  await api.addUrl(token, uni, url.trim());
                  setUrl("");
                  setMsg({ kind: "ok", text: "تمت إضافة الرابط وبدأت فهرسته." });
                })
              }
            >
              إضافة وفهرسة
            </button>
          </div>
          <p className="muted small">لا يُسمح بروابط خارج النطاق الرسمي، ولا ببوابات الطلبة أو صفحات تسجيل الدخول.</p>
        </div>

        <div className="card">
          <h2>رفع وثيقة أكاديمية</h2>
          <p className="muted small">الصيغ المدعومة: PDF، TXT، CSV، JSON، XLSX. تُستخرج بيانات المساقات (الرمز، الاسم، الساعات، المتطلبات) تلقائياً إن وُجدت.</p>
          <div style={{ display: "grid", gap: 8 }}>
            <input className="input" type="file" accept=".pdf,.txt,.md,.csv,.json,.xlsx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="الملف" />
            <label className="field">
              الوجهة
              <select className="select" value={target} onChange={(e) => setTarget(e.target.value as "uploads" | "hackathon")}>
                <option value="uploads">الوثائق الأكاديمية المرفوعة للجامعة المختارة</option>
                <option value="hackathon">وثائق الهاكاثون الرسمية (مرتبطة بالجامعة المختارة)</option>
              </select>
            </label>
            <div className="grid2" style={{ gap: 8 }}>
              <input className="input" placeholder="عنوان الوثيقة (اختياري)" value={meta.title} onChange={(e) => setMeta({ ...meta, title: e.target.value })} />
              <input className="input" placeholder="الإصدار / تاريخ الوثيقة (اختياري)" value={meta.version} onChange={(e) => setMeta({ ...meta, version: e.target.value })} />
              <input className="input" placeholder="العام الأكاديمي مثل 2026/2027 (اختياري)" value={meta.academic_year} onChange={(e) => setMeta({ ...meta, academic_year: e.target.value })} />
              <input className="input" placeholder="البرنامج الأكاديمي (اختياري)" value={meta.program} onChange={(e) => setMeta({ ...meta, program: e.target.value })} />
            </div>
            <button
              className="btn"
              disabled={busy || !file}
              onClick={() =>
                guarded(async () => {
                  const f = new FormData();
                  f.append("file", file as File);
                  f.append("university_id", uni);
                  f.append("target", target);
                  Object.entries(meta).forEach(([k, v]) => f.append(k, v));
                  const r = await api.upload(token, f);
                  if (r.status === "indexed")
                    setMsg({
                      kind: "ok",
                      text: `تمت فهرسة الوثيقة: ${r.chunks} مقطع، ${r.courses ?? 0} مساق مستخرج.${r.warnings?.length ? " تنبيه: " + r.warnings.join("؛ ") : ""}`,
                    });
                  else if (r.status === "duplicate") setMsg({ kind: "warn", text: "هذه الوثيقة مفهرسة مسبقاً (محتوى مكرر)، لم تتم إضافتها مرة أخرى." });
                  else setMsg({ kind: "bad", text: `تعذّرت فهرسة الوثيقة: ${r.error}` });
                })
              }
            >
              رفع وفهرسة
            </button>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} className={`tab ${tab === t.id ? "active" : ""}`} onClick={() => setTab(t.id)}>
              {t.label}
              {t.id === "errors" && errors.length > 0 ? ` (${errors.length})` : ""}
            </button>
          ))}
        </div>

        {tab === "web" && renderDocs(webDocs)}
        {tab === "pdf" && renderDocs(pdfDocs)}
        {tab === "uploads" && renderDocs(uploadDocs, true)}

        {tab === "programs" &&
          (programs.length === 0 ? (
            <p className="muted">لا توجد برامج مستخرجة بعد.</p>
          ) : (
            <div className="table-wrap">
              <p className="muted small">
                المصدر:{" "}
                <a href={programs[0].source_url} target="_blank" rel="noopener noreferrer">
                  {safeDecode(programs[0].source_url)}
                </a>{" "}
                — تاريخ الاطلاع: {arDate(programs[0].retrieved_at)}
              </p>
              <table className="data">
                <thead>
                  <tr>
                    <th>التخصص</th>
                    <th>الكلية</th>
                    <th>مكان الدراسة</th>
                    <th>الحد الأدنى للمعدل</th>
                    <th>فرع الثانوية العامة</th>
                    <th>الدرجة</th>
                    <th>العام الأكاديمي</th>
                  </tr>
                </thead>
                <tbody>
                  {programs.map((p) => (
                    <tr key={p.id}>
                      <td>{p.name}</td>
                      <td>{p.faculty}</td>
                      <td>{p.campus}</td>
                      <td>{p.min_average}</td>
                      <td>{p.streams}</td>
                      <td>{p.degree || "—"}</td>
                      <td>{p.academic_year || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}

        {tab === "courses" && (
          <div>
            <input
              className="input"
              placeholder="ابحث برقم المساق أو اسمه…"
              value={courseQ}
              onChange={(e) => setCourseQ(e.target.value)}
              style={{ marginBottom: 10, width: "100%", maxWidth: 360 }}
            />
            {courses.length === 0 ? (
              <p className="muted">لا توجد مساقات مستخرجة.</p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>رقم المساق</th>
                      <th>اسم المساق</th>
                      <th>الساعات</th>
                      <th>المتطلبات كما وردت</th>
                      <th>التصنيف</th>
                      <th>الفصل المقترح</th>
                      <th>المصدر</th>
                    </tr>
                  </thead>
                  <tbody>
                    {courses.map((c) => (
                      <tr key={`${c.doc_id}-${c.code}-${c.category}`}>
                        <td className="ltr">{c.code}</td>
                        <td>{c.title}</td>
                        <td>{c.credits || "—"}</td>
                        <td className="small ltr">{c.requisites_raw || "—"}</td>
                        <td className="small">{c.category || "—"}</td>
                        <td className="small">{c.semester || "—"}</td>
                        <td className="small">
                          <a href={`/api/documents/${c.doc_id}/file#page=${c.page}`} target="_blank" rel="noopener noreferrer">
                            صفحة {c.page}
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {tab === "errors" &&
          (errors.length === 0 ? (
            <p className="muted">لا توجد أخطاء مسجلة.</p>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>الوقت</th>
                    <th>المرحلة</th>
                    <th>الرابط / الملف</th>
                    <th>الرسالة</th>
                  </tr>
                </thead>
                <tbody>
                  {errors.map((e) => (
                    <tr key={e.id}>
                      <td className="small">{arDate(e.created_at, true)}</td>
                      <td>
                        <span className="badge gray">{e.stage}</span>
                      </td>
                      <td className="small title-cell ltr">{e.url ? safeDecode(e.url) : "—"}</td>
                      <td className="err">{e.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}

        {tab === "runs" && (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>#</th>
                  <th>النوع</th>
                  <th>الحالة</th>
                  <th>البداية</th>
                  <th>النهاية</th>
                  <th>مفهرس</th>
                  <th>دون تغيير</th>
                  <th>مكرر</th>
                  <th>متجاهل</th>
                  <th>فشل</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td>{r.id}</td>
                    <td>{r.kind}</td>
                    <td>{statusBadge(r.status)}</td>
                    <td className="small">{arDate(r.started_at, true)}</td>
                    <td className="small">{arDate(r.finished_at, true)}</td>
                    <td>{String(r.summary.indexed ?? (r.summary.status === "indexed" ? 1 : "—"))}</td>
                    <td>{String(r.summary.unchanged ?? "—")}</td>
                    <td>{String(r.summary.duplicate ?? "—")}</td>
                    <td>{String(r.summary.skipped ?? "—")}</td>
                    <td>{String(r.summary.failed ?? (r.summary.status === "failed" ? 1 : "—"))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
