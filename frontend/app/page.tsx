"use client";

import { useEffect, useRef, useState } from "react";
import { api, type ChatResponse, type University } from "@/lib/api";

type Msg = { role: "user"; text: string } | { role: "bot"; res: ChatResponse; id: number };

const EXAMPLES = [
  "شو مواد سنة أولى فصل أول؟",
  "إذا رسبت بمادة البرمجة، بقدر أنزل اللي بعدها؟",
  "شو المتطلب السابق لمادة 0702211؟",
  "كم ساعة لازم أخلص عشان أتخرج؟",
  "شو المواد الاختيارية؟",
  "شو بنوخذ في مادة نظم التشغيل؟",
  "شو متطلبات التسجيل في مشروع التخرج؟",
  "كم ساعة تدريب ميداني لازم؟",
];

// This assistant serves Computer Engineering students at Al-Quds University.
const UNIVERSITY_ID = "alquds";

export default function ChatPage() {
  const [universities, setUniversities] = useState<University[]>([]);
  const [input, setInput] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const counter = useRef(0);

  useEffect(() => {
    api.universities().then(setUniversities).catch((e) => setError(`تعذّر الاتصال بالخادم: ${e.message}`));
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [msgs, busy]);

  async function ask(question: string, uniOverride?: string) {
    const q = question.trim();
    if (!q || busy) return;
    setMsgs((m) => [...m, { role: "user", text: q }]);
    setInput("");
    setBusy(true);
    setError("");
    try {
      const res = await api.chat(q, uniOverride ?? UNIVERSITY_ID);
      counter.current += 1;
      setMsgs((m) => [...m, { role: "bot", res, id: counter.current }]);
    } catch (e) {
      setError(`حدث خطأ أثناء معالجة السؤال: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  function pickUniAndRetry(id: string) {
    const lastQ = [...msgs].reverse().find((m) => m.role === "user") as { text: string } | undefined;
    if (lastQ) ask(lastQ.text, id);
  }

  return (
    <div className="chat-shell">
      <section className="card">
        <h1>مرشد طلبة هندسة الحاسوب</h1>
        <p className="muted small" style={{ margin: 0 }}>
          جامعة القدس — كلية الهندسة. اسأل عن مساقات الخطة الدراسية، المتطلبات السابقة، توزيع المواد على
          الفصول، المواد الاختيارية، ومتطلبات التخرج. أجيب فقط من الخطة الدراسية الرسمية، وإذا لم أجد الإجابة
          سأخبرك بذلك.
        </p>
      </section>

      {error && <div className="banner bad">{error}</div>}

      <section className="messages" aria-live="polite">
        {msgs.length === 0 && (
          <div className="card">
            <h2>جرّب أحد هذه الأسئلة</h2>
            <div className="examples">
              {EXAMPLES.map((e) => (
                <button key={e} className="chip" onClick={() => ask(e)}>
                  {e}
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="msg user">
              {m.text}
            </div>
          ) : (
            <BotMessage key={i} res={m.res} universities={universities} onPickUni={pickUniAndRetry} />
          ),
        )}
        {busy && <div className="typing">جارٍ البحث في المصادر الرسمية…</div>}
        <div ref={endRef} />
      </section>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          ask(input);
        }}
      >
        <textarea
          className="textarea"
          placeholder="اكتب سؤالك بالعربية أو باللهجة… مثال: شو المتطلب السابق لمادة هياكل البيانات؟"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              ask(input);
            }
          }}
          aria-label="سؤالك"
        />
        <button className="btn" type="submit" disabled={busy || !input.trim()}>
          إرسال
        </button>
      </form>
    </div>
  );
}

function BotMessage({
  res,
  universities,
  onPickUni,
}: {
  res: ChatResponse;
  universities: University[];
  onPickUni: (id: string) => void;
}) {
  const cls = res.status === "no_answer" ? "refusal" : res.status === "conflict" ? "conflict" : "";
  return (
    <div className={`msg bot card ${cls}`}>
      <div className="status-line">
        {res.university && <span className="badge">{res.university.name}</span>}
        {res.status === "conflict" && <span className="badge bad">تعارض بين المصادر</span>}
        {res.status === "no_answer" && <span className="badge warn">لا توجد إجابة</span>}
        {res.status === "needs_university" && <span className="badge warn">يلزم تحديد الجامعة</span>}
      </div>
      <div className="answer">
        {res.answer}
      </div>
      {res.status === "needs_university" && universities.length > 0 && (
        <div className="row" style={{ marginTop: 10 }}>
          {universities.map((u) => (
            <button key={u.id} className="btn ghost sm" onClick={() => onPickUni(u.id)}>
              {u.name_ar}
            </button>
          ))}
        </div>
      )}
      {res.notice && <div className="notice">{res.notice}</div>}
    </div>
  );
}
