"""Requirement-aware scoring: posting sections, years/degree/knockout extraction, the candidate profile, and the
explained score."""
import copy
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import candidate, db, main, master, scoring, search
from app.jobparse import degree_required, jd_sections, knockouts_in, seniority_level, years_required
from tests.conftest import SAMPLE_MASTER, make_job

NVIDIA_STYLE = """NVIDIA is looking for a Senior Data Engineer to join our platform team.
What you’ll be doing:
• Build streaming pipelines with Kafka and Spark.
What we need to see:
• 8+ years of experience building data platforms.
• 5+ years of experience with Python.
• BS in Computer Science or equivalent experience.
Ways to stand out from the crowd:
• Experience with Kubernetes.
NVIDIA is widely considered to be one of the technology world’s most desirable employers. We offer 401(k) and Snowflake credits.
NVIDIA is committed to fostering a diverse work environment and proud to be an equal opportunity employer."""


# ---------------------------------------------------------------- sections
def test_sections_follow_the_headings():
    kinds = [k for k, _ in jd_sections(NVIDIA_STYLE)]
    assert kinds == ["intro", "responsibilities", "required", "preferred", "boilerplate"]
    text = dict(jd_sections(NVIDIA_STYLE))
    assert "Kubernetes" in text["preferred"] and "8+ years" in text["required"]


@pytest.mark.parametrize("heading, kind", [
    ("Minimum Qualifications:", "required"), ("Required Experiences & Skills", "required"),
    ("What You Bring", "required"), ("You're our person if…", "required"), ("Basic qualifications", "required"),
    ("Preferred Qualifications", "preferred"), ("Nice-to-haves", "preferred"), ("Bonus points", "preferred"),
    ("Even better if:", "preferred"), ("Key Responsibilities", "responsibilities"),
    ("What you'll actually be building", "responsibilities"), ("Your Impact", "responsibilities"),
    ("About Salesforce", "boilerplate"), ("Benefits & Perks", "boilerplate"), ("Posting Statement", "boilerplate"),
    ("Pay range for this role:", "boilerplate"), ("About the team", "other"), ("Team culture:", "other"),
])
def test_heading_kinds(heading, kind):
    assert [k for k, _ in jd_sections(f"{heading}\nSome line of text here.")] == [kind]


def test_boilerplate_sentences_are_caught_without_a_heading():
    secs = jd_sections("Requirements:\n5 years with Go.\nWe are an equal opportunity employer, without regard to race.")
    assert [k for k, _ in secs] == ["required", "boilerplate"]


def test_keywords_weigh_by_section():
    kws = scoring.extract_keywords(NVIDIA_STYLE, title="Senior Data Engineer", company="NVIDIA")
    assert kws["Python"]["where"] == "required" and kws["Kubernetes"]["where"] == "preferred"
    assert kws["Apache Spark"]["where"] == "responsibilities"
    assert "Snowflake" not in kws  # only in the boilerplate ("we offer ... Snowflake credits")
    assert kws["Python"]["weight"] > kws["Kubernetes"]["weight"]


# ---------------------------------------------------------------- what the posting requires
@pytest.mark.parametrize("line, years", [
    ("5+ years of experience with Python", 5), ("At least 3 years of professional experience", 3),
    ("Minimum of five years of hands-on experience", 5), ("3-5 years experience in data engineering", 3),
    ("10 yrs industry experience", 10), ("Over 12 years of direct experience in storage software", 12),
])
def test_years_required(line, years):
    found = years_required(jd_sections(f"Requirements:\n{line}."))
    assert [y["years"] for y in found] == [years] and found[0]["kind"] == "required"


@pytest.mark.parametrize("text", [
    "Requirements:\nOur company was founded 20 years ago.",
    "Requirements:\nWe have 30 years of customer trust.",
    "About us:\n10+ years of experience building great teams.",  # boilerplate
])
def test_years_ignored(text):
    assert years_required(jd_sections(text)) == []


@pytest.mark.parametrize("line, level", [
    ("Bachelor's degree in Computer Science required.", 1),
    ("BS or MS degree in Computer Engineering.", 1),
    ("Master’s degree in Statistics.", 2),
    ("Must be pursuing a PhD in Computer Science.", 3),
    ("Bachelor's degree in CS or equivalent experience.", None),
    ("5+ years proven experience; or master's degree.", None),
    ("MBA or relevant advanced degree preferred.", None),
])
def test_degree_required(line, level):
    d = degree_required(jd_sections(f"Requirements:\n{line}"))
    assert (d["level"] if d else None) == level


@pytest.mark.parametrize("text, kinds", [
    ("We are unable to provide visa sponsorship for this role.", ["sponsorship"]),
    ("Candidates must be authorized to work without sponsorship.", ["sponsorship"]),
    ("This role is not eligible for visa sponsorship.", ["sponsorship"]),
    ("Must be a U.S. Citizen operating on U.S. soil.", ["citizenship"]),
    ("US citizenship required; active security clearance preferred or ability to obtain", ["citizenship"]),
    ("Must hold an active Top Secret clearance.", ["clearance"]),
    ("the ability to obtain a Public Trust clearance.", ["clearance"]),
    ("We hire regardless of race, color, citizenship or national origin.", []),
    ("U.S. Citizenship or Permanent Residency required.", []),
])
def test_knockouts(text, kinds):
    assert [k["kind"] for k in knockouts_in(text)] == kinds


@pytest.mark.parametrize("title, level", [
    ("Software Engineer Intern", 0), ("Junior Data Analyst", 1), ("Software Engineer I", 1), ("Data Engineer", 2),
    ("Software Engineer II", 2), ("Senior Data Engineer", 3), ("Sr. Analyst", 3), ("Staff Engineer", 4),
    ("Senior Staff Engineer", 4), ("Engineering Manager", 4), ("Principal Engineer", 5), ("Senior Manager, Data", 5),
    ("Associate Director of Analytics", 6), ("VP, Engineering", 7), ("", None),
])
def test_seniority_level(title, level):
    assert seniority_level(title) == level


# ---------------------------------------------------------------- the candidate profile
@pytest.mark.parametrize("text, end, want", [
    ("Jan 2020", False, (2020, 1)), ("March, 2021", False, (2021, 3)), ("03/2019", False, (2019, 3)),
    ("Summer 2015", False, (2015, 7)), ("2018", False, (2018, 1)), ("2018", True, (2018, 12)), ("", False, None),
    ("Present", True, (date.today().year, date.today().month)),
])
def test_parse_when(text, end, want):
    assert candidate.parse_when(text, end=end) == want


def _resume(*jobs, degree="B.S. Computer Science"):
    data = copy.deepcopy(SAMPLE_MASTER)
    data["sections"][2]["entries"] = [
        {"title": t, "company": c, "start": s, "end": e, "bullets": bullets} for t, c, s, e, bullets in jobs]
    data["sections"][3]["entries"] = [{"degree": degree, "school": "UT Austin"}] if degree else []
    return data


def test_profile_counts_years_once_and_per_skill():
    data = _resume(("Senior Data Engineer", "A", "Jan 2020", "Dec 2022", ["Built Spark jobs in Python."]),
                   ("Data Engineer", "B", "Jan 2019", "Dec 2020", ["Wrote SQL reports."]),  # overlaps 2020
                   ("Analyst", "C", "Jan 2015", "Dec 2016", ["Excel models."]))
    p = candidate.profile(data, {"needs_sponsorship": "Yes"})
    assert p["years_total"] == 6.0  # 2019-2022 (4 years, overlap once) + 2015-2016 (2 years)
    assert p["years_by_skill"]["Python"] == 3.0 and p["years_by_skill"]["SQL"] == 2.0
    assert p["recent_title"] == "Senior Data Engineer" and p["level"] == 3
    assert p["degree"] == 1 and p["needs_sponsorship"] is True
    assert "Built Spark jobs in Python." in p["bullets"]


def test_profile_without_dates_or_education():
    data = _resume(("Engineer", "A", "", "", ["Did things."]), degree=None)
    p = candidate.profile(data)
    assert p["years_total"] is None and p["degree"] is None and p["has_education"] is False
    assert candidate.profile(None)["bullets"] == []


# ---------------------------------------------------------------- the explained score
RESUME_TEXT = "Senior Data Engineer. Python, SQL, Spark, Kafka, Airflow. Built streaming pipelines."


def _prof(years=("Jan 2018", "Present"), title="Senior Data Engineer", degree="B.S. Computer Science", **applicant):
    data = _resume((title, "Acme", years[0], years[1], ["Built streaming pipelines with Kafka and Spark in Python."]),
                   degree=degree)
    return candidate.profile(data, applicant)


def test_benefits_only_match_scores_low():
    jd = ("We are hiring an Accountant.\nRequirements:\nMonth-end close and reconciliations in Excel.\n"
          "Benefits:\nOur benefits team uses Python, SQL, Spark and Kafka to run payroll.")
    sc = scoring.score(RESUME_TEXT, jd, "Accountant", profile=_prof())
    assert sc["matched"] == [] and sc["score"] < 20


def test_experience_gap_is_explained():
    jd = "Senior Data Engineer\nRequirements:\n10+ years of experience building data platforms.\n5+ years with Kafka."
    three = _prof(years=(f"Jan {date.today().year - 3}", "Present"))
    sc = scoring.score(RESUME_TEXT, jd, "Senior Data Engineer", profile=three)
    assert sc["experience"]["need"] == 10 and 3 <= sc["experience"]["have"] <= 4
    kinds = [a["kind"] for a in sc["adjustments"]]
    assert "experience" in kinds and "skill_years" in kinds
    assert sc["score"] == max(0, sc["base"] + sum(a["points"] for a in sc["adjustments"]))
    enough = scoring.score(RESUME_TEXT, jd, "Senior Data Engineer", profile=_prof(years=("Jan 2010", "Present")))
    assert "experience" not in [a["kind"] for a in enough["adjustments"]] and enough["score"] > sc["score"]


def test_seniority_gap():
    jd = "Requirements:\nPython and Spark."
    up = scoring.score(RESUME_TEXT, jd, "Director of Data Engineering", profile=_prof(title="Data Engineer"))
    assert up["seniority"]["job"] == 6 and [a["points"] for a in up["adjustments"]] == [-8]
    same = scoring.score(RESUME_TEXT, jd, "Senior Data Engineer", profile=_prof())
    assert same["adjustments"] == []


def test_sponsorship_knockout_is_flagged_only_if_you_need_it():
    jd = "Requirements:\nPython and Spark.\nWe are unable to sponsor visas for this role."
    needs = scoring.score(RESUME_TEXT, jd, "Data Engineer", profile=_prof(needs_sponsorship="Yes"))
    assert [(k["kind"], k["blocking"]) for k in needs["knockouts"]] == [("sponsorship", True)]
    assert any(a["kind"] == "knockout" and a["points"] == -10 for a in needs["adjustments"])
    fine = scoring.score(RESUME_TEXT, jd, "Data Engineer", profile=_prof(needs_sponsorship="No"))
    assert [(k["kind"], k["blocking"]) for k in fine["knockouts"]] == [("sponsorship", False)]
    assert fine["score"] > needs["score"]
    assert search.blocking_knockouts(needs) == [{"kind": "sponsorship", "label": "no visa sponsorship"}]


def test_degree_and_clearance_knockouts():
    jd = "Requirements:\nMaster's degree in Computer Science.\nMust hold an active Secret clearance."
    sc = scoring.score(RESUME_TEXT, jd, "Data Engineer", profile=_prof(has_clearance="No"))
    assert {k["kind"]: k["blocking"] for k in sc["knockouts"]} == {"clearance": True, "degree": True}
    assert any(a["kind"] == "knockout" and a["points"] == -20 for a in sc["adjustments"])
    unknown = scoring.score(RESUME_TEXT, jd, "Data Engineer", profile=_prof(degree=None))
    assert {k["kind"]: k["blocking"] for k in unknown["knockouts"]} == {"clearance": False}  # no education listed


def test_requirement_evidence():
    sc = scoring.score(RESUME_TEXT, NVIDIA_STYLE, "Senior Data Engineer", company="NVIDIA", profile=_prof())
    ev = {e["text"]: e for e in sc["evidence"]}
    python = ev["5+ years of experience with Python."]
    assert python["status"] == "met" and "Python" in python["evidence"] and python["kind"] == "required"
    assert ev["Experience with Kubernetes."]["status"] == "missing"
    assert ev["Experience with Kubernetes."]["kind"] == "preferred"


# ---------------------------------------------------------------- storage, list filter, API
def test_rescore_stores_knockouts_and_list_can_hide_them():
    db.upsert_jobs([make_job("a:sponsor", description_text="Requirements:\nPython.\nWe cannot sponsor visas."),
                    make_job("a:ok", description_text="Requirements:\nPython and SQL.")])
    master.save(data=SAMPLE_MASTER, filename="cv.txt")
    db.save_settings({"profile": {**db.get_settings()["profile"], "needs_sponsorship": "Yes"}})
    search.rescore_all()
    assert db.get_job("a:sponsor")["knockouts"] == [{"kind": "sponsorship", "label": "no visa sponsorship"}]
    assert db.get_job("a:ok")["knockouts"] == []
    s = db.get_settings()
    assert {j["id"] for j in search.list_jobs(s)} == {"a:sponsor", "a:ok"}
    s["filters"]["hide_knockouts"] = True
    assert [j["id"] for j in search.list_jobs(s)] == ["a:ok"]


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        c.get(f"/?key={main.SESSION_KEY}", follow_redirects=False)
        yield c


def test_detail_explains_the_score_and_feedback_is_stored(client):
    db.upsert_job(make_job(description_text=NVIDIA_STYLE, title="Senior Data Engineer"))
    master.save(data=SAMPLE_MASTER, filename="cv.txt")
    a = client.get("/api/jobs/acme/external:R1/detail").json()["analysis"]
    assert a["evidence"] and a["seniority"]["job"] == 3 and a["experience"]["need"] == 8
    assert a["where"]["Python"] == "required"
    assert client.post("/api/jobs/acme/external:R1/feedback", json={"value": 1}).json() == {"feedback": 1}
    assert db.get_job("acme/external:R1")["feedback"] == 1
    assert client.post("/api/jobs/acme/external:R1/feedback", json={"value": 0}).json() == {"feedback": None}
    assert client.post("/api/jobs/acme/external:R1/feedback", json={"value": 5}).status_code == 422


def test_profile_change_rescores(client):
    db.upsert_job(make_job(description_text="Requirements:\nPython.\nWe cannot sponsor visas."))
    master.save(data=SAMPLE_MASTER, filename="cv.txt")
    search.rescore_all()
    assert db.get_job("acme/external:R1")["knockouts"] == []
    client.put("/api/settings", json={"profile": {"needs_sponsorship": "Yes"}})
    assert search.rescorer.wait(10)
    assert db.get_job("acme/external:R1")["knockouts"][0]["kind"] == "sponsorship"


def test_new_scoring_version_rescores_at_startup():
    db.upsert_job(make_job())
    master.save(data=SAMPLE_MASTER, filename="cv.txt")
    db.save_settings({"scoring_version": 1})
    with TestClient(main.app):
        assert search.rescorer.wait(10)
    assert db.get_settings()["scoring_version"] == scoring.VERSION
    assert db.get_job("acme/external:R1")["match_score"] is not None


# ---------------------------------------------------------------- tools/calibrate.py
def _calibrate():
    import importlib.util
    from tests.conftest import ROOT
    spec = importlib.util.spec_from_file_location("calibrate", ROOT / "tools" / "calibrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_calibration_finds_the_weights_that_separate_outcomes():
    cal = _calibrate()
    assert cal.auc([(90, True), (10, False)]) == 1.0 and cal.auc([(10, True), (90, False)]) == 0.0
    assert cal.auc([(50, True), (50, False)]) == 0.5 and cal.auc([(1, True)]) is None
    # here only the title alignment tells good from poor matches
    rows = [(50, 50, 90, 0, True), (60, 40, 80, 0, True), (40, 60, 95, 0, True),
            (55, 50, 10, 0, False), (60, 45, 20, 0, False), (45, 55, 5, 0, False)]
    results = cal.fit(rows)
    by_weights = {w: a for a, w in results}
    assert results[0][0] == 1.0
    assert results[0][1] == cal.CURRENT  # equally good splits: the current weights are kept
    assert by_weights[(100, 0, 0)] < 1.0 and by_weights[(0, 0, 100)] == 1.0  # coverage alone can't tell them apart
    assert cal.label({"feedback": -1}, {"interviewing"}) is False  # your thumbs down wins
    assert cal.label({}, {"rejected", "screening"}) is True  # got a screen: the match was good
    assert cal.label({}, {"applied"}) is None


# ---------------------------------------------------------------- the same inputs always give the same score
def test_present_means_the_day_the_resume_was_saved_not_today():
    data = copy.deepcopy(SAMPLE_MASTER)
    saved = date(2025, 1, 15)
    a = candidate.profile(data, None, saved)
    b = candidate.profile(data, None, date(2027, 1, 15))
    assert b["years_total"] > a["years_total"]  # asking "as of" another day does change the years...
    stored = {"data": data, "uploaded_at": "2025-01-15T09:00:00"}
    settings = {"profile": {}}
    assert candidate.profile_for(stored, settings)["years_total"] == a["years_total"]  # ...but the stored resume is fixed
    assert candidate.profile_for(None, settings) is None


def test_score_does_not_depend_on_the_clock_or_the_search_keywords():
    stored = {"data": copy.deepcopy(SAMPLE_MASTER), "uploaded_at": "2025-01-15T09:00:00"}
    prof = candidate.profile_for(stored, {"profile": {}})
    first = scoring.score(RESUME_TEXT, NVIDIA_STYLE, "Senior Data Engineer", company="NVIDIA", profile=prof)
    from unittest import mock
    with mock.patch("app.candidate.date") as fake:
        fake.today.return_value = date(2031, 6, 1)
        fake.fromisoformat = date.fromisoformat
        later = scoring.score(RESUME_TEXT, NVIDIA_STYLE, "Senior Data Engineer", company="NVIDIA",
                              profile=candidate.profile_for(stored, {"profile": {}}))
    assert later == first
