import type { Metadata } from "next";
import { IBM_Plex_Sans_Arabic } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const plex = IBM_Plex_Sans_Arabic({ subsets: ["arabic", "latin"], weight: ["400", "500", "600", "700"], display: "swap" });

export const metadata: Metadata = {
  title: "UniGuide AI — مرشد هندسة الحاسوب",
  description: "مرشد أكاديمي لطلبة هندسة الحاسوب في جامعة القدس يجيب من الخطة الدراسية الرسمية",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ar" dir="rtl">
      <body className={plex.className}>
        <header className="topbar">
          <div className="topbar-inner">
            <Link href="/" className="brand">
              <span className="brand-mark" aria-hidden>
                ◆
              </span>
              <span>
                UniGuide <b>AI</b> <span className="muted small">· هندسة الحاسوب</span>
              </span>
            </Link>
            <nav className="nav">
              <Link href="/">المحادثة</Link>
              <Link href="/sources">مصادر المعلومات الأكاديمية</Link>
            </nav>
          </div>
        </header>
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
