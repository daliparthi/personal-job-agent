import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from docx import Document

from app import apply, config, db
from tests.conftest import SAMPLE_MASTER, make_job

WORKDAY_TOP = NS(parent_frame=None, url="https://acme.wd1.myworkdayjobs.com/en-US/External/apply")


@pytest.mark.parametrize("frame, want", [
    (WORKDAY_TOP, True),
    (NS(parent_frame=None, url="https://wd3.myworkdaysite.com/recruiting/acme/X"), True),
    (NS(parent_frame=None, url="https://login.example.com/?next=myworkdayjobs.com"), False),
    (NS(parent_frame=WORKDAY_TOP, url="https://acme.wd1.myworkdayjobs.com/frame"), False),  # not the top frame
    (NS(parent_frame=None, url="about:blank"), False),
    (None, False),
])
def test_trusted_frame(frame, want):
    assert apply.trusted_frame(frame) is want


def test_binding_answers_only_trusted_frames():
    w = apply.BrowserWorker()
    page = object()
    w.pages[page] = {"job_id": "acme/external:R1", "profile": {"first_name": "Jordan"}, "folder": None}
    db.upsert_job(make_job())

    def call(frame, kind, payload=None, pg=page):
        return asyncio.run(w._on_event({"page": pg, "frame": frame}, kind, payload))

    assert call(WORKDAY_TOP, "profile") == {"first_name": "Jordan"}
    assert call(NS(parent_frame=None, url="https://evil.example/"), "profile") is None
    assert call(WORKDAY_TOP, "profile", pg=object()) is None  # a page Job Agent did not open
    assert call(NS(parent_frame=None, url="https://evil.example/"), "applied") is None
    assert db.get_job("acme/external:R1")["status"] == "new"
    assert call(WORKDAY_TOP, "applied", "detected") is True
    assert db.get_job("acme/external:R1")["status"] == "applied"


class FakePage:
    def __init__(self):
        self.url = None
        self.clicked = []
        self.handlers = {}

    def on(self, event, fn):
        self.handlers[event] = fn

    async def goto(self, url, wait_until=None):
        self.url = url

    async def bring_to_front(self):
        pass

    def locator(self, selector):
        page = self

        class First:
            async def click(self, timeout=None):
                page.clicked.append(selector)

        return NS(first=First())


@pytest.mark.parametrize("url, workday", [
    ("https://acme.wd1.myworkdayjobs.com/External/job/Austin-TX/Data-Engineer_R1", True),
    ("https://job-boards.greenhouse.io/globex/jobs/101", False),
    ("https://jobs.lever.co/hooli/5f1c2a3b", False),
])
def test_apply_window_autofills_workday_only(url, workday, monkeypatch):
    """Other job boards open in the apply window, but nothing is clicked, typed or uploaded there."""
    w = apply.BrowserWorker()
    page = FakePage()

    async def context():
        async def new_page():
            return page
        return NS(new_page=new_page)

    started = []

    async def loop(*args):
        started.append(args)

    monkeypatch.setattr(w, "_context", context)
    monkeypatch.setattr(w, "_upload_loop", loop)
    monkeypatch.setattr(w, "_account_loop", loop)
    account = {"email": "jordan.avery@example.com", "password": "test-only"}

    async def go():
        ok = await w._open("j:1", url, {"first_name": "Jordan"}, "resume.docx", None, account)
        await asyncio.sleep(0)  # let ensure_future'd loops start
        return ok

    assert asyncio.run(go()) is True and page.url == url
    assert bool(page.clicked) is workday and bool(started) is workday
    assert w.pages[page]["job_id"] == "j:1"


@pytest.mark.parametrize("raw, want", [
    ('Data Engineer: "Platform" / ML?', "Data Engineer Platform ML"),
    ("  ...  ", "Untitled"),
    ("a" * 200, "a" * 80),
    (None, "Untitled"),
])
def test_safe_name(raw, want):
    assert apply.safe_name(raw).replace("  ", " ") == want.replace("  ", " ")


@pytest.fixture
def fake_pdf(monkeypatch):
    rendered = []

    async def render(html, out_path):
        rendered.append(html)
        out_path.write_bytes(b"%PDF-1.4 test")

    monkeypatch.setattr(apply.worker, "render_pdf", render)
    return rendered


def _package(job, **kw):
    args = dict(resume=SAMPLE_MASTER, score_before=40, score_after=55, approved=["Spark"], rejected=["Go"],
                profile={"first_name": "Jordan", "last_name": "Avery"})
    args.update(kw)
    return asyncio.run(apply.save_package(job, **args))


def test_save_package_writes_every_file(fake_pdf):
    db.upsert_job(make_job())
    r = _package(db.get_job("acme/external:R1"))
    folder = config.APPLICATIONS / "Acme" / "Data Engineer - R1"
    assert r["folder"] == str(folder) and r["pdf_error"] is None
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["Jordan_Avery_Resume.docx", "Jordan_Avery_Resume.pdf", "Jordan_Avery_Resume.txt",
                     "application.json", "job_description.html", "job_description.txt", "tailored_resume.yaml"]
    assert Document(folder / "Jordan_Avery_Resume.docx").paragraphs[0].text == "Jordan Avery"
    meta = json.loads((folder / "application.json").read_text(encoding="utf-8"))
    assert meta["status"] == "prepared" and meta["match_score_after"] == 55 and meta["approved_keywords"] == ["Spark"]
    assert meta["files"]["resume_pdf"] == "Jordan_Avery_Resume.pdf"
    assert "Data Engineer" in (folder / "job_description.html").read_text(encoding="utf-8")
    assert "font-variant-ligatures" in fake_pdf[0]


def test_pdf_failure_still_saves_the_package(monkeypatch):
    async def boom(html, out_path):
        raise RuntimeError("no browser")
    monkeypatch.setattr(apply.worker, "render_pdf", boom)
    db.upsert_job(make_job())
    r = _package(db.get_job("acme/external:R1"), profile={})
    assert r["pdf"] is None and r["pdf_error"] == "no browser"
    meta = json.loads((config.APPLICATIONS / "Acme" / "Data Engineer - R1" / "application.json").read_text("utf-8"))
    assert meta["files"]["resume_pdf"] is None and meta["files"]["resume_docx"] == "Tailored_Resume.docx"


def test_mark_applied_and_repackage_keeps_applied(fake_pdf):
    db.upsert_job(make_job())
    r = _package(db.get_job("acme/external:R1"))
    apply.mark_applied("acme/external:R1", r["folder"], how="manual")
    assert db.get_job("acme/external:R1")["status"] == "applied"
    _package(db.get_job("acme/external:R1"))
    apps = apply.list_applications()
    assert len(apps) == 1 and apps[0]["status"] == "applied" and apps[0]["applied_how"] == "manual"
    assert apps[0]["folder"] == r["folder"]


def test_open_folder_only_inside_applications(tmp_path):
    with pytest.raises(ValueError):
        apply.open_folder(str(tmp_path))
    with pytest.raises(ValueError):
        apply.open_folder(str(config.APPLICATIONS / ".." / ".."))
    with pytest.raises(ValueError):
        apply.open_personal("nonsense")


def test_jd_html_escapes_metadata():
    html = apply._jd_html(make_job(title="<script>x</script>", company="A&B"))
    assert "&lt;script&gt;" in html and "A&amp;B" in html and "<script>x" not in html.split("<hr>")[0]
