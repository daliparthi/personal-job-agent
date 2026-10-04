"""Cover letters and short answers: the posting digest they are written from, the files in the package, the API,
and attaching the letter where an application form asks for one."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from docx import Document

from app import apply, config, db
from app.jobparse import jd_digest
from app.resume_io import answers_text, clean_letter, letter_text, long_date
from tests.conftest import SAMPLE_MASTER, make_job
from tests.test_api import JOB, JOB_URL

LETTER = {
    "greeting": "Dear Acme hiring team,",
    "paragraphs": ["I am writing to apply for the Data Engineer position at Acme.",
                   "As Senior Data Engineer at Northwind Analytics, I designed Spark and Airflow pipelines.",
                   "  ",
                   "Thank you for your time and consideration."],
    "signoff": "Sincerely,", "name": "Jordan Avery", "date": "2026-10-04",
    "answers": [{"key": "why_role", "question": "Why are you interested in the Data Engineer role?",
                 "text": "It lines up with the work I have done."},
                {"key": "why_company", "question": "Why Acme?", "text": ""}],
    "changes": {"paragraphs.1": {"orig": "x", "alt": "y", "state": "alt"}},
}


# ---------------------------------------------------------------- what the letter is written from
JD = """About Acme
Acme builds analytics software used by 400 hospitals. We care about patient outcomes above everything else.
To get the best candidate experience, please apply to at most three roles.
What you'll do
• Build batch pipelines in Python
• Own data quality across the warehouse
Responsibilities include:
• Mentor engineers
Requirements
• 5+ years of Python
Acme is an equal opportunity employer and considers applicants without regard to race.
"""


def test_jd_digest_keeps_the_postings_own_words():
    d = jd_digest(JD)
    assert d["about"] == ["Acme builds analytics software used by 400 hospitals.", "We care about patient outcomes above everything else."]
    assert d["duties"] == ["Build batch pipelines in Python", "Own data quality across the warehouse", "Mentor engineers"]
    assert jd_digest("") == {"about": [], "duties": []}


# ---------------------------------------------------------------- the files
def test_clean_letter():
    c = clean_letter(LETTER)
    assert c["paragraphs"] == [p for p in LETTER["paragraphs"] if p.strip()]
    assert c["answers"] == [{"question": "Why are you interested in the Data Engineer role?",
                             "text": "It lines up with the work I have done."}]
    assert "changes" not in c
    assert clean_letter(None) is None and clean_letter({"paragraphs": ["", " "]}) is None
    assert clean_letter({"paragraphs": [1, "Hello there."]})["signoff"] == "Sincerely,"


def test_letter_text_layout():
    text = letter_text(clean_letter(LETTER), SAMPLE_MASTER)
    assert text.startswith("Jordan Avery\nAustin, TX | (555) 010-0199 | jordan.avery@example.com")
    assert "\n\nOctober 4, 2026\n\nDear Acme hiring team,\n\nI am writing to apply" in text
    assert text.endswith("Thank you for your time and consideration.\n\nSincerely,\nJordan Avery\n")
    assert long_date("2026-01-09") == "January 9, 2026" and long_date("soon") == "soon"
    assert answers_text(clean_letter(LETTER), {"title": "Data Engineer", "company": "Acme"}) == (
        "Short answers for Data Engineer at Acme\n\nWhy are you interested in the Data Engineer role?\n"
        "It lines up with the work I have done.\n")


@pytest.fixture
def fake_pdf(monkeypatch):
    rendered = []

    async def render(html, out_path):
        rendered.append(html)
        out_path.write_bytes(b"%PDF-1.4 test")

    monkeypatch.setattr(apply.worker, "render_pdf", render)
    return rendered


def _package(letter, **kw):
    db.upsert_job(make_job())
    args = dict(resume=SAMPLE_MASTER, score_before=40, score_after=55, approved=[], rejected=[],
                profile={"first_name": "Jordan", "last_name": "Avery"}, letter=letter)
    args.update(kw)
    return asyncio.run(apply.save_package(db.get_job("acme/external:R1"), **args))


def test_package_includes_the_cover_letter(fake_pdf):
    r = _package(LETTER)
    folder = config.APPLICATIONS / "Acme" / "Data Engineer - R1"
    names = sorted(p.name for p in folder.iterdir())
    assert {"Jordan_Avery_Cover_Letter.docx", "Jordan_Avery_Cover_Letter.pdf", "Jordan_Avery_Cover_Letter.txt",
            "Short_Answers.txt"} <= set(names)
    assert r["cover_letter"]["docx"] == str(folder / "Jordan_Avery_Cover_Letter.docx")
    assert r["cover_letter"]["pdf"] == str(folder / "Jordan_Avery_Cover_Letter.pdf")
    paras = [p.text for p in Document(folder / "Jordan_Avery_Cover_Letter.docx").paragraphs]
    assert paras[0] == "Jordan Avery" and "Dear Acme hiring team," in paras and paras[-1] == "Jordan Avery"
    assert "As Senior Data Engineer at Northwind Analytics" in (folder / "Jordan_Avery_Cover_Letter.txt").read_text("utf-8")
    assert any("Dear Acme hiring team," in html for html in fake_pdf)
    meta = json.loads((folder / "application.json").read_text(encoding="utf-8"))
    assert meta["files"]["cover_letter_docx"] == "Jordan_Avery_Cover_Letter.docx"
    assert meta["files"]["cover_letter_pdf"] == "Jordan_Avery_Cover_Letter.pdf"
    assert meta["files"]["short_answers"] == "Short_Answers.txt"


def test_package_without_a_letter(fake_pdf):
    r = _package(None, profile={})
    folder = config.APPLICATIONS / "Acme" / "Data Engineer - R1"
    assert r["cover_letter"] is None
    assert not [p for p in folder.iterdir() if "Cover_Letter" in p.name or p.name == "Short_Answers.txt"]
    meta = json.loads((folder / "application.json").read_text(encoding="utf-8"))
    assert meta["files"]["cover_letter_docx"] is None and meta["files"]["short_answers"] is None


def test_letter_pdf_failure_keeps_the_other_files(monkeypatch):
    async def boom(html, out_path):
        raise RuntimeError("no browser")
    monkeypatch.setattr(apply.worker, "render_pdf", boom)
    r = _package(LETTER, profile={})
    assert r["cover_letter"]["pdf"] is None and r["cover_letter"]["pdf_error"] == "no browser"
    assert r["cover_letter"]["docx"].endswith("Tailored_Cover_Letter.docx")


def test_api_saves_the_letter_with_the_tailored_resume_and_packages_it(client, fake_pdf):
    db.upsert_job(make_job())
    db.save_resume("cv", "Python SQL", SAMPLE_MASTER, 0)
    assert set(client.get(f"{JOB_URL}/detail").json()["digest"]) == {"about", "duties"}
    doc = {"resume": SAMPLE_MASTER, "changes": {}, "letter": LETTER}
    client.put(f"{JOB_URL}/tailored", json={"doc": doc, "score_after": 50, "approved": [], "rejected": []})
    assert db.get_tailored(JOB)["doc"]["letter"]["greeting"] == "Dear Acme hiring team,"
    r = client.post(f"{JOB_URL}/package", json={"doc": doc, "score_after": 50, "launch": False}).json()
    assert r["cover_letter"]["docx"].endswith("_Cover_Letter.docx")


# ---------------------------------------------------------------- attaching the letter on Workday
@pytest.mark.parametrize("label, want", [
    ("Resume/CV Upload a file or drag and drop here", "resume"),
    ("Upload your CV", "resume"),
    ("Cover Letter (optional) Select files", "cover_letter"),
    ("coverLetter", "cover_letter"),
    ("Letter of interest", "cover_letter"),
    ("Resume and cover letter", "resume"),  # one field for both: the resume
    ("Transcript", "other"),
    ("", "other"),
])
def test_upload_kind(label, want):
    assert apply.upload_kind(label) == want


class FakeInputs:
    def __init__(self, labels):
        self.labels = labels
        self.files = {}

    async def count(self):
        return len(self.labels)

    def nth(self, i):
        inputs = self

        class One:
            async def evaluate(self, js):
                return inputs.labels[i]

            async def set_input_files(self, path):
                inputs.files[i] = path

        return One()


class FakeUploadPage:
    def __init__(self, labels, uploaded=0):
        self.url = "https://acme.wd1.myworkdayjobs.com/External/job/R1/apply?step=2"
        self.inputs = FakeInputs(labels)
        self.uploaded = uploaded

    def locator(self, selector):
        if selector == 'input[type="file"]':
            return self.inputs
        return NS(count=self._uploaded)

    async def _uploaded(self):
        return self.uploaded


def _attach(page, cover="letter.docx", done=None):
    done = set() if done is None else done
    asyncio.run(apply.BrowserWorker._attach_files(page, "resume.docx", cover, done))
    return done


def test_resume_and_cover_letter_go_where_their_labels_say():
    page = FakeUploadPage(["Cover Letter", "Resume/CV"])
    done = _attach(page)
    assert page.inputs.files == {0: "letter.docx", 1: "resume.docx"}
    page.inputs.files.clear()
    _attach(page, done=done)  # once per page
    assert page.inputs.files == {}


def test_unlabelled_upload_gets_the_resume_only():
    page = FakeUploadPage(["Upload a file"])
    _attach(page)
    assert page.inputs.files == {0: "resume.docx"}


def test_no_cover_letter_no_upload_and_existing_resume_kept():
    page = FakeUploadPage(["Resume", "Cover letter"], uploaded=1)
    _attach(page, cover=None)
    assert page.inputs.files == {}  # a resume is already attached; no letter was written
