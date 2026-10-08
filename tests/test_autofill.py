"""Deeper Workday autofill: the answer bank, the data the page gets, and autofill.js itself running in a real
(headless) browser against the Workday-shaped pages in tests/fixtures/workday_pages/.

The fixture pages are hand-built and anonymized: they follow Workday's markup (data-automation-id names, listbox
dropdowns, numbered "Work Experience 1" groups, "Add" buttons) rather than being copies of a real tenant's pages."""
import asyncio
import re
from types import SimpleNamespace as NS

import pytest

from app import answers, apply, db, formfill
from tests.conftest import FIXTURES, SAMPLE_MASTER

PAGES = FIXTURES / "workday_pages"
HOST = "https://acme.wd1.myworkdayjobs.com"
TOP = NS(parent_frame=None, url=f"{HOST}/en-US/External/job/R1/apply")

MASTER = {
    "name": "Jordan Avery",
    "sections": [
        {"title": "Experience", "kind": "experience", "entries": [
            {"title": "Senior Data Engineer", "company": "Northwind Analytics", "location": "Austin, TX",
             "start": "Mar 2021", "end": "Present", "bullets": ["Designed Spark pipelines.", "Migrated a warehouse."]},
            {"title": "Data Analyst", "company": "Fabrikam", "location": "Dallas, TX", "start": "2016", "end": "02/2021",
             "bullets": ["Built SQL reports."]},
        ]},
        {"title": "Education", "kind": "education", "entries": [
            {"degree": "B.S. Computer Science", "school": "University of Texas at Austin", "start": "2012", "end": "2016"},
        ]},
    ],
}
PROFILE = {"first_name": "Jordan", "last_name": "Avery", "email": "jordan.avery@example.com", "phone": "555-010-0199",
           "phone_type": "Mobile", "address1": "1 Test Street", "city": "Austin", "state": "TX",
           "postal_code": "78701", "country": "United States of America", "authorized_us": "Yes",
           "needs_sponsorship": "No", "previously_employed": "No", "how_heard": "Company Website",
           "account_ready": False}
JOB = {"company": "Acme", "title": "Data Engineer"}


def settings(**over):
    s = db.get_settings()
    s["autofill"] = {**s["autofill"], **over.pop("autofill", {})}
    s["disclosures"] = {**s["disclosures"], **over.pop("disclosures", {})}
    return s


# ---------------------------------------------------------------- the answer bank
@pytest.mark.parametrize("a, b, same", [
    ("Why do you want to work at Acme?", "Why do you want to work for Acme?", True),
    ("What are your salary expectations?", "What are your salary expectations? *", True),
    ("Are you willing to relocate?", "Are you willing to relocate? (Required)", True),
    ("Are you legally authorized to work in the United States?", "Are you legally authorized to work in Canada?", False),
    ("Will you now require sponsorship?", "Will you in the future require sponsorship?", False),
    ("How many years of Python experience do you have?", "How many years of Java experience do you have?", False),
    ("Do you have a non-compete agreement?", "Do you not have a non-compete agreement?", False),
])
def test_question_matching(a, b, same):
    s = answers.similarity(answers.normalize(a, "Acme"), answers.normalize(b, "Acme"))
    assert (s >= answers.MATCH) is same


def test_normalize_replaces_this_postings_company_and_title():
    assert answers.normalize("Why do you want to work at Acme Corp?*", "Acme Corp") == "why do you want to work at {company}"
    assert answers.normalize("Why the Data Engineer role?", "", "Data Engineer") == "why the {title} role"


@pytest.mark.parametrize("question, answer, keep", [
    ("What are your salary expectations?", "$150,000", True),
    ("Are you willing to relocate?", "Yes", True),
    ("Gender", "Female", False),
    ("Please select your ethnicity", "Asian", False),
    ("Veteran Status", "I am not a protected veteran", False),
    ("Address Line 1", "1 Test Street", False),
    ("Phone Number", "555", False),
    ("Today's Date", "10/04/2026", False),
    ("Are you willing to relocate to another state?", "Yes", True),
    ("Why?", "Because", False),
    ("Are you willing to relocate?", "Select One", False),
])
def test_what_is_remembered(question, answer, keep):
    assert answers.rememberable(question, answer) is keep


def test_capture_lookup_and_seed():
    a1 = answers.capture("What are your salary expectations?", "$150,000", company="Acme")
    a2 = answers.capture("Why do you want to work at Globex?", "Globex builds what I use every day.", company="Globex")
    assert a1 and a2 and answers.capture("Gender", "Female") is None
    # the same question again replaces the answer
    assert answers.capture("What are your salary expectations? *", "$160,000", company="Acme") == a1
    assert db.answer_get(a1)["answer"] == "$160,000"
    got = answers.lookup(["What are your salary expectations?", "Why do you want to work at Acme?", "Gender",
                          "Are you legally authorized to work in Canada?"], company="Acme")
    assert got[0]["answer"] == "$160,000" and got[0]["exact"]
    assert got[1]["answer"].startswith("Globex builds") and "written for Globex" in got[1]["note"]
    assert got[2] is None and got[3] is None
    # cover-letter drafts are added, but never replace an answer you gave on a form
    letter = {"answers": [{"question": "Why do you want to work at Acme?", "text": "A draft."},
                          {"question": "What relevant experience do you have for this role?", "text": "Spark."}]}
    assert answers.seed_from_letter(letter, "Acme", "Data Engineer") == 1
    assert db.answer_get(a2)["answer"].startswith("Globex builds")
    draft = answers.lookup(["What relevant experience do you have for this role?"], "Initech")[0]
    assert draft["answer"] == "Spark." and "cover-letter draft" in draft["note"]


def test_answer_bank_api(client):
    r = client.post("/api/answers", json={"question": "Are you willing to relocate?", "answer": "Yes", "kind": "choice"})
    assert r.status_code == 200 and r.json()["kind"] == "choice"
    aid = r.json()["id"]
    assert client.post("/api/answers", json={"question": "Gender", "answer": "Female"}).status_code == 400
    r = client.put(f"/api/answers/{aid}", json={"answer": "Open to discussion"})
    assert r.json()["answer"] == "Open to discussion"
    other = client.post("/api/answers", json={"question": "What are your salary expectations?", "answer": "$1"}).json()
    assert client.put(f"/api/answers/{other['id']}", json={"question": "Are you willing to relocate?"}).status_code == 409
    assert [a["id"] for a in client.get("/api/answers").json()] == [other["id"], aid]
    assert client.delete(f"/api/answers/{aid}").json() == {"ok": True}
    assert client.delete(f"/api/answers/{aid}").status_code == 404
    s = client.put("/api/settings", json={"autofill": {"add_entries": True}, "disclosures": {"gender": "skip"}}).json()
    assert s["autofill"]["add_entries"] is True and s["autofill"]["experience"] is True
    assert s["disclosures"] == {"gender": "skip", "ethnicity": "decline", "veteran": "decline", "disability": "decline"}
    assert client.put("/api/settings", json={"disclosures": {"gender": "Female"}}).status_code == 422


# ---------------------------------------------------------------- what the page gets
def test_history_from_the_master_resume():
    exp = formfill.experience(MASTER)
    assert exp[0] == {"title": "Senior Data Engineer", "company": "Northwind Analytics", "location": "Austin, TX",
                      "start_year": 2021, "start_month": 3, "end_year": None, "end_month": None, "current": True,
                      "description": "• Designed Spark pipelines.\n• Migrated a warehouse."}
    assert (exp[1]["start_year"], exp[1]["start_month"], exp[1]["end_year"], exp[1]["end_month"]) == (2016, None, 2021, 2)
    edu = formfill.education(MASTER)[0]
    assert edu["school"] == "University of Texas at Austin" and edu["field"] == "Computer Science"
    assert edu["degree_options"][0] == "Bachelor's Degree" and (edu["start_year"], edu["end_year"]) == (2012, 2016)
    assert formfill.education({"sections": [{"kind": "education", "entries": [
        {"degree": "Master of Science in Statistics", "school": "X"}]}]})[0]["field"] == "Statistics"


def test_page_setup():
    s = formfill.page_setup(PROFILE, settings(autofill={"experience": False}), MASTER, JOB)
    assert s["history"] == {"experience": [], "education": [], "skills": []}
    assert s["rules"]["version"] == 1 and s["options"] == {"add_entries": False, "answers": True, "capture": True}
    assert s["disclosures"]["gender"] == "decline" and s["job"] == JOB
    assert len(formfill.page_setup(PROFILE, settings(), MASTER, JOB)["history"]["experience"]) == 2


TAILORED = {
    "name": "Jordan Avery",
    "sections": [
        {"title": "Skills", "kind": "skills", "groups": [
            {"name": "Languages", "items": ["Python", "SQL"]}, {"name": "Big data", "items": ["Apache Spark", "python"]}],
         "lines": ["Cloud: AWS, Azure"]},
        {"title": "Experience", "kind": "experience", "entries": [
            {"title": "Senior Data Engineer", "company": "Northwind Analytics", "location": "Austin, TX",
             "start": "Mar 2021", "end": "Present", "bullets": ["Tailored Spark bullet."]}]},
    ],
}


def test_history_comes_from_the_tailored_resume():
    s = formfill.page_setup(PROFILE, settings(), MASTER, JOB, resume=TAILORED)
    assert s["history"]["experience"][0]["description"] == "• Tailored Spark bullet."
    assert s["history"]["skills"] == ["Python", "SQL", "Apache Spark", "AWS", "Azure"]  # in order, no duplicates
    assert formfill.page_setup(PROFILE, settings(), MASTER, JOB)["history"]["skills"] == []  # the master has none
    assert formfill.page_setup(PROFILE, settings(autofill={"experience": False}), MASTER, JOB,
                               resume=TAILORED)["history"]["skills"] == []


def test_binding_events():
    w = apply.BrowserWorker()
    page = object()
    setup = formfill.page_setup(PROFILE, settings(), MASTER, JOB)
    w.pages[page] = {"job_id": "acme/external:R1", "profile": PROFILE, "folder": None, "setup": setup}

    def call(kind, payload=None, frame=TOP):
        return asyncio.run(w._on_event({"page": page, "frame": frame}, kind, payload))

    assert call("setup")["job"] == JOB
    aid = call("answer", {"question": "What are your salary expectations?", "answer": "$150,000"})
    assert db.answer_get(aid)["company"] == "Acme"
    assert call("answers", ["What are your salary expectations?", "Gender"])[0]["answer"] == "$150,000"
    assert call("answers", ["What are your salary expectations?"], frame=NS(parent_frame=None, url="https://evil.example/")) is None
    assert call("used", [aid, "x"]) is True and db.answer_get(aid)["uses"] == 1
    setup["options"]["capture"] = False
    assert call("answer", {"question": "Are you willing to relocate?", "answer": "Yes"}) is None
    setup["options"]["answers"] = False
    assert call("answers", ["What are your salary expectations?"]) == []


# ---------------------------------------------------------------- autofill.js in a real browser
async def _browser(pw):
    errors = []
    for name, kw in apply.browser_candidates():
        try:
            return await pw.chromium.launch(headless=True, **kw)
        except Exception as e:
            errors.append(f"{name}: {str(e).splitlines()[0][:80]}")
    pytest.skip("no Chrome, Edge or Chromium to run autofill.js in: " + "; ".join(errors))


async def _serve(route):
    name = route.request.url.split("?")[0].rsplit("/", 1)[-1]
    path = PAGES / (name if "." in name else f"{name}.html")
    if not path.exists():
        return await route.fulfill(status=404, body="not found")
    await route.fulfill(body=path.read_text(encoding="utf-8"),
                        content_type="text/javascript" if path.suffix == ".js" else "text/html")


def run_page(name, setup, done_js, then=None, timeout=20, folder=None):
    """Open fixture page `name` on a Workday host with autofill.js, wait until done_js is true, then return
    {"values": {id: value}, "buttons": {id: text}, "checked": [...], "clicks": [...], "banner": text, ...}."""
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as pw:
            browser = await _browser(pw)
            try:
                ctx = await browser.new_context()
                w = apply.BrowserWorker()
                await w.prepare(ctx)
                await ctx.route(f"{HOST}/**", _serve)
                page = await ctx.new_page()
                w.pages[page] = {"job_id": "acme/external:R1", "profile": setup["profile"], "folder": folder,
                                 "setup": setup}
                await page.goto(f"{HOST}/en-US/External/job/R1/apply/{name}")
                try:
                    await page.wait_for_function(done_js, timeout=timeout * 1000)
                finally:
                    if then:
                        await then(page)
                    state = await page.evaluate("""() => ({
                      values: Object.fromEntries([...document.querySelectorAll('input[type=text], input:not([type]), textarea')]
                        .map((e) => [e.id, e.value])),
                      buttons: Object.fromEntries([...document.querySelectorAll('button[aria-haspopup=listbox]')]
                        .map((b) => [b.id, b.textContent.trim()])),
                      checked: [...document.querySelectorAll('input:checked')].map((e) => e.id),
                      outlined: [...document.querySelectorAll('*')].filter((e) => e.style.outlineColor).map((e) => e.id || e.tagName),
                      chips: [...document.querySelectorAll('[data-automation-id=selectedItem]')]
                        .filter((e) => !e.closest('#popup')).map((e) => e.textContent),
                      clicks: window.__clicks,
                      banner: document.getElementById('__jobagent_banner')?.innerText || '',
                    })""")
                return state
            finally:
                await browser.close()

    return asyncio.run(go())


def _setup(**over):
    return formfill.page_setup(PROFILE, settings(**over), MASTER, JOB)


NEVER = {"Save and Continue", "Next", "Submit", "Delete"}


def test_my_information_fills_profile_and_lists_whats_left():
    st = run_page("my_information", _setup(),
                  "() => document.getElementById('devtype').textContent === 'Mobile' && "
                  "document.getElementById('__jobagent_missing')?.innerText.includes('Middle Name')")
    v, b = st["values"], st["buttons"]
    assert (v["first"], v["last"], v["addr1"], v["city"], v["zip"], v["phone"]) == (
        "Jordan", "Avery", "1 Test Street", "Austin", "78701", "555-010-0199")
    assert v["pref"] == "" and v["middle"] == ""  # preferred / middle names are not first names
    assert b == {"source": "Company Website", "country": "United States of America", "state": "Texas", "devtype": "Mobile"}
    assert "prev-no" in st["checked"]
    assert "Still to fill on this page (1)" in st["banner"] and "Middle Name" in st["banner"]
    assert not NEVER & set(st["clicks"])


def test_a_field_you_clear_or_retype_is_not_filled_again():
    async def edit(page):  # empty one filled field and retype another, then give autofill several more passes
        await page.click("#city", click_count=3)  # selects the text on every platform (Ctrl+A is Cmd+A on macOS)
        await page.keyboard.press("Delete")
        await page.fill("#first", "Jo")
        await page.click("h2")
        await page.wait_for_timeout(4500)

    st = run_page("my_information", _setup(),
                  "() => document.getElementById('devtype').textContent === 'Mobile' && "
                  "document.getElementById('__jobagent_missing')?.innerText.includes('Middle Name')", then=edit)
    v = st["values"]
    assert v["city"] == "" and v["first"] == "Jo"  # left as you edited them
    assert v["last"] == "Avery" and v["zip"] == "78701"  # the fields you didn't touch were still filled


def test_my_experience_adds_and_fills_entries_when_allowed():
    st = run_page("my_experience", _setup(autofill={"add_entries": True}),
                  "() => document.getElementById('edu1-to')?.value === '2016' && "
                  "document.getElementById('exp2-to-y')?.value === '2021'")
    v, b = st["values"], st["buttons"]
    assert (v["exp1-title"], v["exp1-company"], v["exp1-loc"]) == ("Senior Data Engineer", "Northwind Analytics", "Austin, TX")
    assert (v["exp1-from-m"], v["exp1-from-y"], v["exp1-to-m"], v["exp1-to-y"]) == ("03", "2021", "", "")
    assert "exp1-current" in st["checked"] and "exp2-current" not in st["checked"]
    assert v["exp1-desc"] == "• Designed Spark pipelines.\n• Migrated a warehouse."
    assert (v["exp2-title"], v["exp2-from-m"], v["exp2-from-y"], v["exp2-to-m"], v["exp2-to-y"]) == (
        "Data Analyst", "", "2016", "02", "2021")  # no month on the resume: none is made up
    assert (v["edu1-school"], v["edu1-field"], v["edu1-from"], v["edu1-to"]) == (
        "University of Texas at Austin", "Computer Science", "2012", "2016")
    assert b["edu1-degree"] == "Bachelor's Degree"
    assert v["skills"] == ""
    assert "web1" not in v  # the Websites section's Add was never clicked
    assert st["clicks"].count("Add") == 2 and st["clicks"].count("Add Another") == 1  # 2 jobs + 1 school
    assert not NEVER & set(st["clicks"])


def test_my_experience_clicks_nothing_by_default():
    async def settle(page):
        await page.wait_for_timeout(3500)

    st = run_page("my_experience", _setup(),
                  "() => document.getElementById('__jobagent_msg')?.innerText.includes('click Add')", then=settle)
    assert st["clicks"] == []
    assert "Your resume has 2 jobs and 1 school" in st["banner"]


def test_questions_use_the_answer_bank_and_remember_new_answers():
    answers.capture("What are your salary expectations?", "$150,000 base", company="Globex")
    answers.capture("Are you willing to relocate?", "Open to discussion", kind="choice", company="Globex")
    answers.capture("Do you have a non-compete agreement with a current employer?", "No", kind="choice")
    answers.capture("Why do you want to work at Globex?", "Globex makes what I use.", company="Globex")
    answers.capture("Are you legally authorized to work in Canada?", "No")

    async def answer_q7(page):  # you type an answer to a question the bank doesn't know
        await page.fill("#q7", "6")
        await page.click("h2")
        await page.wait_for_timeout(500)

    st = run_page("questions", _setup(),
                  "() => document.getElementById('q4').textContent !== 'Select One' && "
                  "document.getElementById('q6-no').checked && document.getElementById('q5').value", then=answer_q7)
    v, b = st["values"], st["buttons"]
    assert (b["q1"], b["q2"]) == ("Yes", "No")  # profile: authorized, no sponsorship
    assert v["q3"] == "$150,000 base" and b["q4"] == "Open to discussion" and "q6-no" in st["checked"]
    assert v["q5"] == "Globex makes what I use."  # matched as "...work at {company}", flagged as written for Globex
    assert v["q8"] == "No"  # stored under Canada; the US question above is a different one
    assert {"q3", "q4", "q5"} <= set(st["outlined"])
    assert "from your answer bank" in st["banner"]
    saved = {a["question"]: a for a in db.answers_all()}
    assert saved["How many years of Python experience do you have?"]["answer"] == "6"
    assert saved["How many years of Python experience do you have?"]["company"] == "Acme"
    assert saved["What are your salary expectations?"]["uses"] == 1
    assert not NEVER & set(st["clicks"])


def test_voluntary_disclosures_decline_and_leave_consent_alone():
    st = run_page("disclosures", _setup(disclosures={"veteran": "skip"}),
                  "() => document.getElementById('ethnicity').textContent !== 'Select One'")
    b = st["buttons"]
    assert b["gender"] == "I do not wish to answer" and b["ethnicity"] == "I do not wish to answer"
    assert b["veteran"] == "Select One"  # set to "leave for me"
    assert "terms" not in st["checked"]
    assert not NEVER & set(st["clicks"])
    assert not db.answers_all()  # nothing about you is stored


@pytest.mark.parametrize("question, key", [
    ("Are you legally authorized to work in the United States?", "authorized_us"),
    ("Do you have authorization to work in the United States?", "authorized_us"),
    ("Are you currently eligible to work in the U.S.?", "authorized_us"),
    ("What is your work authorization status in the US?", "authorized_us"),
    ("Will you now or in the future require sponsorship for employment visa status?", "needs_sponsorship"),
    ("Do you have temporary authorization (e.g., OPT, CPT) to currently work in the United States?", None),
    ("Are you legally authorized to work in Canada?", None),
])
def test_which_profile_answer_a_work_authorization_question_gets(question, key):
    rules = formfill.load_rules()["choices"]
    hit = next((k for k, pattern, exclude in rules if re.search(pattern, question, re.I)
                and not (exclude and re.search(exclude, question, re.I))), None)
    assert hit == key


def test_voluntary_disclosures_pick_the_answers_you_chose():
    st = run_page("disclosures", _setup(disclosures={"gender": "male", "ethnicity": "asian", "veteran": "not_veteran"}),
                  "() => document.getElementById('veteran').textContent !== 'Select One'")
    assert st["buttons"] == {"gender": "Male", "ethnicity": "Asian", "veteran": "I am not a protected veteran"}
    assert "terms" not in st["checked"]
    assert not NEVER & set(st["clicks"])


def test_a_chosen_answer_the_form_does_not_offer_is_left_alone():
    async def settle(page):
        await page.wait_for_timeout(3500)  # several passes: the other two stay as they are

    st = run_page("disclosures", _setup(disclosures={"gender": "female", "ethnicity": "native", "veteran": "skip"}),
                  "() => document.getElementById('gender').textContent !== 'Select One'", then=settle)
    assert st["buttons"]["gender"] == "Female"
    assert st["buttons"]["ethnicity"] == "Select One"  # no such option on this form: not replaced by "decline"
    assert st["buttons"]["veteran"] == "Select One"


def test_self_identify_ticks_yes_when_you_have_a_disability():
    st = run_page("self_identify", _setup(disclosures={"disability": "yes"}), "() => document.getElementById('dis-yes').checked")
    assert st["checked"] == ["dis-yes"]
    assert st["values"]["sig-name"] == "" and st["values"]["sig-date"] == ""


def test_skills_are_typed_in_and_picked_from_workdays_suggestions():
    setup = formfill.page_setup(PROFILE, settings(), MASTER, JOB, resume={"sections": [{"kind": "skills", "groups": [
        {"name": "Tools", "items": ["Python", "Spark", "COBOL", "SQL"]}]}]})
    st = run_page("my_experience", setup,
                  "() => document.querySelectorAll('[data-automation-id=selectedItem]').length === 3", timeout=40)
    # "Python" is listed as "Python (Programming Language)"; "Spark" has two suggestions (the one starting with it wins);
    # COBOL has none, so nothing is picked
    assert st["chips"] == ["Python (Programming Language)", "Spark Streaming", "SQL"]
    assert st["values"]["skills"] == ""  # nothing is left typed in the box
    assert not db.answers_all()


def test_skills_are_picked_from_this_searchs_list_and_checked():
    """On a real form the last skill's suggestions ("ETL Development" lists "Data Transformation" too) stay on screen
    while the next search loads, and a click on them is lost: each skill must be picked from its own search's list,
    and it must end up selected."""
    setup = formfill.page_setup(PROFILE, settings(), MASTER, JOB, resume={"sections": [{"kind": "skills", "groups": [
        {"name": "Data", "items": ["ETL Development", "Data Transformation"]}]}]})
    st = run_page("skills", setup, "() => document.querySelectorAll('#chips [data-automation-id=selectedItem]').length === 2"
                  " && document.getElementById('skills').value === ''", timeout=30)
    assert st["chips"] == ["ETL Development", "Data Transformation"]
    assert st["values"]["skills"] == ""


def test_every_skill_is_tried_even_though_workday_puts_the_cursor_back_in_the_box(tmp_path):
    """Workday focuses the skills box after each pick (a trusted event): that must not count as you typing there, or
    only the first few skills get added. Its rows ignore a bare scripted click, so each skill is picked with the real
    mouse. Spelling differences ("Fine Tuning" / "Fine-Tuning", "Data Pipelines" / "Data Pipeline") and a bracketed
    note ("Data Cataloging (Alation)") still find the skill; a chip already picked ("Data Quality Management") is not a
    suggestion for another skill ("Data Quality"); the ones Workday doesn't list are named in the banner and the log."""
    names = ["ETL Development", "Data Transformation", "Fine Tuning", "Data Cataloging (Alation)", "Python", "SQL",
             "Information Stewardship", "Snowflake", "Tableau", "Power BI", "Data Governance",
             "Data Quality Management (Bigeye)", "Data Quality", "Data Pipelines"]
    setup = formfill.page_setup(PROFILE, settings(), MASTER, JOB, resume={"sections": [{"kind": "skills", "groups": [
        {"name": "Data", "items": names}]}]})
    st = run_page("skills", setup, "() => document.querySelectorAll('#chips [data-automation-id=selectedItem]').length === 12"
                  " && /added 12 of 14/.test(document.getElementById('__jobagent_banner')?.innerText || '')",
                  timeout=120, folder=tmp_path)
    assert st["chips"] == ["ETL Development", "Data Transformation", "Fine-Tuning", "Data Cataloging",
                           "Python (Programming Language)", "SQL", "Snowflake", "Tableau (Software)", "Power BI",
                           "Data Governance", "Data Quality Management", "Data Pipeline"]
    assert "Skills: added 12 of 14; Workday doesn't list Information Stewardship, Data Quality." in st["banner"]
    assert st["values"]["skills"] == ""
    log = (tmp_path / apply.SKILLS_LOG).read_text(encoding="utf-8")
    assert "added: Fine Tuning -> Fine-Tuning" in log and "added: Data Pipelines -> Data Pipeline" in log
    assert "not listed: Information Stewardship" in log and "NOT SELECTED" not in log
    assert not db.answers_all()  # Job Agent's own clicks on the list are not answers you gave


def test_the_real_mouse_clicks_only_a_list_suggestion():
    """autofill.js may ask for a real mouse click, but only on a suggestion in a list: never a button or a link."""
    pytest.importorskip("playwright")
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as pw:
            browser = await _browser(pw)
            try:
                page = await browser.new_page()
                await page.set_content("""<button id=b style="display:block;height:30px">Submit</button>
                  <a id=a href="#x" style="display:block;height:30px">Link</a>
                  <div role=listbox><div role=option id=o style="height:30px">Python</div></div>
                  <ul data-automation-id=selectedItemList><li><div role=option data-automation-id=selectedItem id=c
                    style="height:30px">SQL</div></li></ul>
                  <script>window.hits = []; document.addEventListener('click', (e) => hits.push(e.target.id));</script>""")
                got = {}
                for el in ("b", "a", "o", "c"):
                    box = await page.locator(f"#{el}").bounding_box()
                    xy = {"x": box["x"] + 5, "y": box["y"] + box["height"] / 2}
                    got[el] = await apply.BrowserWorker._click_option(page, xy)
                assert await apply.BrowserWorker._click_option(page, {"x": "nan", "y": 1}) is False
                # the list was redrawn and another skill is at that spot now: not clicked
                box = await page.locator("#o").bounding_box()
                xy = {"x": box["x"] + 5, "y": box["y"] + box["height"] / 2}
                assert await apply.BrowserWorker._click_option(page, {**xy, "name": "sql"}) is False
                assert await apply.BrowserWorker._click_option(page, {**xy, "name": "python"}) is True
                return got, await page.evaluate("window.hits")
            finally:
                await browser.close()

    got, hits = asyncio.run(go())
    assert got == {"b": False, "a": False, "o": True, "c": False}
    assert hits == ["o", "o"]


def test_self_identify_declines_disability_and_leaves_the_signature():
    st = run_page("self_identify", _setup(), "() => document.getElementById('dis-skip').checked")
    assert st["checked"] == ["dis-skip"]
    assert st["values"]["sig-name"] == "" and st["values"]["sig-date"] == ""
    assert "Name" in st["banner"] and "Today's Date" in st["banner"]


def test_sample_master_works_too():
    assert formfill.experience(SAMPLE_MASTER)[0]["current"] is True
