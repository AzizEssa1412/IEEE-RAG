"""Website fetching policy + main-content extraction (offline, MockTransport)."""
import httpx

from app.ingest.web import (PoliteFetcher, canonical_url, extract_page, is_private_or_unsafe,
                            relevance_score)

DOMAINS = ["www.alquds.edu", "alquds.edu"]

PAGE = """<!doctype html><html dir="rtl"><head><title>دائرة التسجيل - Al-Quds University</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebPage",
"datePublished":"2021-01-01T00:00:00+00:00","dateModified":"2026-04-04T08:34:10+00:00"}]}</script>
</head><body>
<header><nav><a href="/ar/">الرئيسية</a><a href="/ar/news/">الأخبار</a></nav></header>
<main id="page-content">
 <h1>دائرة التسجيل</h1>
 <p>تعتمد الجامعة نظام الساعات المعتمدة للفصل الأول 2026/2027.</p>
 <div class="w-popup w-btn-wrapper"><div class="w-popup-box-content">
   <h5>مسؤولية الطالب</h5><ul><li>الالتزام بتعليمات المرشد الأكاديمي.</li></ul>
 </div></div>
 <table><thead><tr><th>الكلية</th><th>التخصص</th></tr></thead>
  <tbody><tr><td>كلية الهندسة</td><td>هندسة الحاسوب</td></tr></tbody></table>
 <table><thead><tr><th>مكان الدراسة</th><th>التخصص</th><th>الحد الأدنى للمعدل</th></tr></thead><tbody></tbody></table>
 <script>var programsData = [["حرم أبو ديس","علم الحاسوب","75"]];</script>
 <div class="w-grid"><h2><a href="/ar/news-ar/1/">خبر غير أكاديمي</a></h2></div>
 <a href="/wp-content/uploads/2026/07/admission-bulletin.pdf">نشرة القبول</a>
 <a href="/ar/admissions-registration-ar/acad-calender">التقويم الأكاديمي</a>
 <p>Row Backgrounds</p>
</main>
<footer><div class="footer-about">جميع الحقوق محفوظة</div></footer>
</body></html>"""


def test_extract_page_keeps_academic_content_and_drops_navigation():
    pc = extract_page(PAGE, "https://www.alquds.edu/ar/registration/")
    text = pc.text
    assert pc.title == "دائرة التسجيل"
    assert "الساعات المعتمدة" in text
    assert "الالتزام بتعليمات المرشد" in text            # popup content is real content
    assert "الكلية: كلية الهندسة | التخصص: هندسة الحاسوب" in text  # tables preserved
    assert "الرئيسية" not in text and "جميع الحقوق" not in text  # nav + footer removed
    assert "خبر غير أكاديمي" not in text                  # news grid removed
    assert "Row Backgrounds" not in text
    assert pc.date_modified == "2026-04-04T08:34:10+00:00"
    assert pc.academic_year == "2026/2027"
    assert ("https://www.alquds.edu/wp-content/uploads/2026/07/admission-bulletin.pdf",
            "نشرة القبول") in pc.pdf_links


def test_inline_program_data_becomes_a_table_with_real_headers():
    pc = extract_page(PAGE, "https://www.alquds.edu/ar/x/")
    assert pc.data_tables[0]["headers"] == ["مكان الدراسة", "التخصص", "الحد الأدنى للمعدل"]
    assert "التخصص: علم الحاسوب" in pc.text


def test_url_policy():
    assert is_private_or_unsafe("https://www.alquds.edu/wp-login.php")
    assert is_private_or_unsafe("https://www.alquds.edu/ar/portal/student")
    assert is_private_or_unsafe("ftp://www.alquds.edu/file")
    assert not is_private_or_unsafe("https://www.alquds.edu/ar/admissions-registration-ar/")
    assert canonical_url("https://WWW.alquds.edu/ar/x/#top") == "https://www.alquds.edu/ar/x/"
    assert relevance_score("https://www.alquds.edu/ar/admissions-registration-ar/acad-calender") > 0
    assert relevance_score("https://www.alquds.edu/ar/admissions-ar/our-team-ar/") == 0


def _fetcher(handler) -> PoliteFetcher:
    return PoliteFetcher(delay=0, transport=httpx.MockTransport(handler))


def test_robots_disallow_is_respected():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /ar/private/\n")
        return httpx.Response(200, html=PAGE)

    f = _fetcher(handler)
    assert f.fetch("https://www.alquds.edu/ar/ok/", DOMAINS).ok
    res = f.fetch("https://www.alquds.edu/ar/private/page", DOMAINS)
    assert not res.ok and res.stage == "robots"


def test_unreachable_robots_means_no_crawling():
    f = _fetcher(lambda req: httpx.Response(503) if req.url.path == "/robots.txt"
                 else httpx.Response(200, html=PAGE))
    res = f.fetch("https://www.alquds.edu/ar/ok/", DOMAINS)
    assert not res.ok and res.stage == "robots"


def test_access_restricted_and_captcha_are_recorded_not_bypassed():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/ar/forbidden/":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, html="<html><div class='g-recaptcha'></div></html>")

    f = _fetcher(handler)
    r1 = f.fetch("https://www.alquds.edu/ar/forbidden/", DOMAINS)
    r2 = f.fetch("https://www.alquds.edu/ar/captcha/", DOMAINS)
    assert not r1.ok and r1.stage == "access" and "403" in r1.error
    assert not r2.ok and r2.stage == "access"


def test_other_domains_and_redirects_off_domain_are_blocked():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://evil.example/ar/"})

    f = _fetcher(handler)
    assert f.fetch("https://other.example/ar/", DOMAINS).stage == "policy"
    assert f.fetch("https://www.alquds.edu/ar/redirect/", DOMAINS).stage == "policy"
