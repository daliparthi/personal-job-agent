"""Job boards besides Workday: URL detection, normalizing each board's postings, local filtering, and a search over
a mixed company list (tests/fixtures/boards/*.json, served in place of the live APIs)."""
import asyncio
import json
import re
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest

from app import db, pipeline, search, sources, workday
from app.sources import ashby, greenhouse, lever, smartrecruiters
from app.workday import WorkdayClient
from tests.conftest import FIXTURES, SAMPLE_MASTER, make_job
from tests.test_search import ACME_URL, FakeWorkday

BOARDS = FIXTURES / "boards"
TODAY = date.today()


def load(name):
    """A fixture with its dates moved to today: "@DAYS_AGO_n@" -> an ISO timestamp, "@MS_DAYS_AGO_n@" -> epoch ms."""
    raw = (BOARDS / name).read_text(encoding="utf-8")

    def ms(m):
        day = datetime.combine(TODAY - timedelta(days=int(m.group(1))), time(12), tzinfo=timezone.utc)
        return str(int(day.timestamp() * 1000))

    raw = re.sub(r'"@MS_DAYS_AGO_(\d+)@"', ms, raw)
    raw = re.sub(r"@DAYS_AGO_(\d+)@", lambda m: f"{TODAY - timedelta(days=int(m.group(1)))}T12:00:00.000Z", raw)
    return json.loads(raw)


def site(url):
    return sources.parse_site("Co", url)


# ---------------------------------------------------------------- which board a URL belongs to
@pytest.mark.parametrize("url, source, host, board, key", [
    ("https://boards.greenhouse.io/airbnb", "greenhouse", "boards-api.greenhouse.io", "airbnb", "airbnb/greenhouse"),
    ("https://job-boards.greenhouse.io/Airbnb/jobs/8184174", "greenhouse", "boards-api.greenhouse.io", "Airbnb",
     "airbnb/greenhouse"),
    ("https://boards.greenhouse.io/embed/job_board?for=stripe", "greenhouse", "boards-api.greenhouse.io", "stripe",
     "stripe/greenhouse"),
    ("https://boards-api.greenhouse.io/v1/boards/figma/jobs", "greenhouse", "boards-api.greenhouse.io", "figma",
     "figma/greenhouse"),
    ("https://jobs.lever.co/palantir", "lever", "api.lever.co", "palantir", "palantir/lever"),
    ("https://jobs.lever.co/palantir/6ed76ce8-4156/apply", "lever", "api.lever.co", "palantir", "palantir/lever"),
    ("https://jobs.eu.lever.co/acme-eu", "lever", "api.eu.lever.co", "acme-eu", "acme-eu/lever"),
    ("https://api.lever.co/v0/postings/palantir?mode=json", "lever", "api.lever.co", "palantir", "palantir/lever"),
    ("https://jobs.ashbyhq.com/openai", "ashby", "api.ashbyhq.com", "openai", "openai/ashby"),
    ("https://jobs.ashbyhq.com/openai/8fb1615c-34bf", "ashby", "api.ashbyhq.com", "openai", "openai/ashby"),
    ("https://api.ashbyhq.com/posting-api/job-board/openai", "ashby", "api.ashbyhq.com", "openai", "openai/ashby"),
    ("https://jobs.smartrecruiters.com/BoschGroup", "smartrecruiters", "api.smartrecruiters.com", "BoschGroup",
     "boschgroup/smartrecruiters"),
    ("https://careers.smartrecruiters.com/Visa/", "smartrecruiters", "api.smartrecruiters.com", "Visa",
     "visa/smartrecruiters"),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "workday", "nvidia.wd5.myworkdayjobs.com",
     "nvidia", "nvidia/nvidiaexternalcareersite"),
])
def test_parse_site_detects_the_board(url, source, host, board, key):
    s = site(url)
    assert (s.source, s.host, s.tenant, s.key) == (source, host, board, key)
    if source != "workday":
        assert s.site == source


def test_ats_override_for_a_careers_page_on_its_own_domain():
    s = sources.parse_site("Airbnb", "https://careers.airbnb.com/", ats="Greenhouse", board="airbnb")
    assert (s.source, s.host, s.tenant, s.key, s.url) == ("greenhouse", "boards-api.greenhouse.io", "airbnb",
                                                           "airbnb/greenhouse", "https://careers.airbnb.com/")
    # ats: alone works when the URL is the board's own
    assert sources.parse_site("P", "https://jobs.lever.co/palantir", ats="lever").key == "palantir/lever"
    assert sources.parse_site("N", "https://nvidia.wd5.myworkdayjobs.com/X", ats="workday").key == "nvidia/x"


@pytest.mark.parametrize("url, ats, board, message", [
    ("https://careers.example.com/jobs", None, None, "Not a supported job board URL"),
    ("https://careers.airbnb.com/", "greenhouse", None, "Not a Greenhouse job board URL"),
    ("https://jobs.lever.co/palantir", "greenhouse", None, "Not a Greenhouse job board URL"),
    ("https://careers.airbnb.com/", "taleo", "airbnb", "Unknown ats: taleo"),
    ("https://careers.airbnb.com/", None, "airbnb", "board: needs ats:"),
    ("https://careers.example.com/", "workday", None, "Not a Workday career-site URL"),
])
def test_parse_site_errors(url, ats, board, message):
    with pytest.raises(ValueError, match=message):
        sources.parse_site("Co", url, ats, board)


def test_source_of_key():
    assert sources.source_of_key("airbnb/greenhouse") == "greenhouse"
    assert sources.source_of_key("boschgroup/smartrecruiters") == "smartrecruiters"
    assert sources.source_of_key("nvidia/nvidiaexternalcareersite") == "workday"
    assert sources.source_of_key("") == "workday"


# ---------------------------------------------------------------- small helpers
def test_when_and_per_year():
    assert sources.base.when("2026-09-09T04:35:19-04:00") == date(2026, 9, 9)
    assert sources.base.when("2026-10-02T20:38:54.511Z") == date(2026, 10, 2)
    assert sources.base.when(1786469891368) == date(2026, 8, 11)
    assert sources.base.when(None) is None and sources.base.when("soon") is None
    assert sources.base.per_year(60, 75, "per-hour-wage") == (124800, 156000)
    assert sources.base.per_year(200000, 250000, "1 YEAR") == (200000, 250000)
    assert sources.base.per_year(10000, 12000, "monthly") == (120000, 144000)
    assert sources.base.per_year(None, 90000, "") == (90000, 90000)
    assert sources.base.per_year(None, None, "1 YEAR") is None
    assert sources.base.per_year(5, 9, "1 YEAR") is None  # not a salary
    assert sources.base.locations("A; B", None, "A", "  C  ") == ["A", "B", "C"]


@pytest.mark.parametrize("locs, countries, want", [
    (["San Francisco, CA"], [], True),
    (["Remote"], [], True),                      # remote, no country: open to the US
    (["Remote - US"], [], True),
    (["Remote USA "], [], True),
    (["Remote - Bangalore, India"], [], False),
    (["London, United Kingdom"], [], False),
    (["Dublin, Ireland", "Austin, TX"], [], True),
    (["Berlin, Germany"], [], False),
    ([], [], True),
    (["Toronto"], ["Canada"], False),            # the board's country wins over the text
    (["Remote"], ["United States"], True),
    (["London, UK", "New York City"], ["United Kingdom", "United States"], True),
    (["Singapore, Singapore"], ["SG"], False),
    (["Austin, TX"], ["US"], True),
    (["CA-Toronto", "Canada Locations"], [], False),       # "CA" here is Canada, not California
    (["IN - Bengaluru", "India Locations"], [], False),    # and "IN" is India, not Indiana
    (["Chicago, US-Remote, Canada-Remote"], [], True),
    (["Amsterdam, Netherlands", "Remote - Colorado"], [], True),
    (["Albuquerque, New Mexico"], [], True),
    (["AMER", "United Kingdom Locations"], [], False),
    (["NYC", "US"], [], True),
    (["Madrid"], [], False),
    (["Toronto"], [], False),
])
def test_in_us(locs, countries, want):
    assert sources.in_us({"locations": locs, "countries": countries}) is want


# ---------------------------------------------------------------- normalizing each board's postings
def test_greenhouse_posting():
    jobs = [greenhouse.posting(j) for j in load("greenhouse_jobs.json")["jobs"]]
    p = jobs[0]
    assert p["ref"] == "101" and p["title"] == "Senior Data Engineer" and p["req_id"] == "GX-101"
    assert p["url"] == "https://job-boards.greenhouse.io/globex/jobs/101"
    assert p["locations"] == ["San Francisco, CA"] and p["remote_raw"] == "Hybrid"
    assert p["posted"] == TODAY - timedelta(days=1)
    assert p["html"].startswith("<h3>What you&#39;ll do</h3>")  # Greenhouse escapes its HTML once more
    assert jobs[2]["remote_raw"] == "" and jobs[2]["locations"] == ["Remote"]  # metadata: null


def test_lever_posting_joins_the_lists_into_the_description():
    p = lever.posting(load("lever_postings.json")[0])
    assert p["title"] == "Data Engineer" and p["countries"] == ["US"] and p["remote_raw"] == "onsite"
    assert p["url"] == "https://jobs.lever.co/hooli/5f1c2a3b-0000-4000-8000-000000000001"
    assert "<h3>What We Require</h3><ul><li>3+ years of Python and SQL</li></ul>" in p["html"]
    assert p["html"].endswith("<div>Hooli is an equal opportunity employer.</div>")
    assert p["salary"] == (124800, 156000)  # $60-75 an hour, a year
    assert p["posted"] == TODAY - timedelta(days=2)


def test_ashby_posting():
    board = load("ashby_board.json")["jobs"]
    p = ashby.posting(board[0])
    assert p["locations"] == ["London, UK", "New York City"]
    assert p["countries"] == ["United Kingdom", "United States"]
    assert p["salary"] == (200000, 250000) and p["salary_text"] == "$200K - $250K"
    assert p["employment"] == "FullTime" and p["remote_raw"] == "Hybrid" and p["posted"] == TODAY
    assert ashby.posting(board[1])["salary"] is None


def test_smartrecruiters_posting_and_detail():
    s = site("https://jobs.smartrecruiters.com/Initech")
    p = smartrecruiters.posting(s, load("smartrecruiters_postings.json")["content"][0])
    assert p["html"] is None  # the description takes a detail request
    assert p["locations"] == ["Detroit, MI, United States"] and p["countries"] == ["us"]
    assert p["remote_raw"] == "Hybrid" and p["req_id"] == "REF7441" and p["employment"] == "Full-time"

    async def go():
        client = WorkdayClient(transport=httpx.MockTransport(FakeBoards()))
        try:
            return await smartrecruiters.detail(client, s, "7440001")
        finally:
            await client.close()

    d = asyncio.run(go())
    assert d["active"] is True and d["url"].endswith("/7440001-software-engineer-python")
    assert d["html"].index("Company Description") < d["html"].index("Job Description") < d["html"].index("Qualifications")


# ---------------------------------------------------------------- local filtering and the stored row
def test_select_applies_the_window_location_and_keywords():
    board = [greenhouse.posting(j) for j in load("greenhouse_jobs.json")["jobs"]]
    picked = sources.select(board, 7, ["python"], [])
    assert [p["ref"] for p in picked] == ["101", "103"]  # 102 London, 104 too old, 105 no "python"
    assert [p["ref"] for p in sources.select(board, 7, [], ["dbt", "office"])] == ["103", "105"]
    assert [p["ref"] for p in sources.select(board, 60, ["python", "aws"], [])] == ["101"]
    # a board that searches sends no description: the keywords are checked after detail()
    sr = {"ref": "1", "title": "Technician", "locations": ["Charleston, SC"], "countries": ["us"], "html": None,
          "posted": TODAY}
    assert sources.select([sr], 7, ["python"], []) == [sr]


def test_select_leaves_out_postings_with_an_omit_word():
    board = [greenhouse.posting(j) for j in load("greenhouse_jobs.json")["jobs"]]
    assert [p["ref"] for p in sources.select(board, 7, ["python"], [], omit=["dbt"])] == ["101"]  # 103 mentions dbt
    assert [p["ref"] for p in sources.select(board, 7, ["python"], [], omit=[])] == ["101", "103"]


def test_to_job_matches_the_workday_row():
    s = site("https://job-boards.greenhouse.io/globex")
    job = sources.to_job(s, "Globex", greenhouse.posting(load("greenhouse_jobs.json")["jobs"][0]))
    wd = make_job()
    assert set(job) == set(wd) | {"source"}  # the same columns a Workday posting fills
    assert job["id"] == "globex/greenhouse:101" and job["company_key"] == "globex/greenhouse"
    assert (job["tenant"], job["site"], job["source"], job["external_path"]) == ("globex", "greenhouse", "greenhouse", "101")
    assert job["states_json"] == ["CA"] and job["remote_type"] == "Hybrid" and job["employment_type"] == "Full-time"
    assert (job["salary_min"], job["salary_max"]) == (150000, 190000)  # from the pay-transparency text
    assert job["posted_date"] == (TODAY - timedelta(days=1)).isoformat()
    assert "5+ years of experience with Python" in job["description_text"]


@pytest.mark.parametrize("raw, want", [
    ({"employment": "FullTime", "time_type": "FullTime"}, ("Full-Time", "Full-time")),
    ({"employment": "PartTime", "time_type": "PartTime"}, ("Part-Time", "Part-time")),
    ({"employment": "Intern", "time_type": "Intern"}, ("Intern", "Internship")),
    ({"employment": "Contractor", "time_type": "Contractor"}, ("Contractor", "Contract")),
    ({"employment": "Fixed-Term", "time_type": "Fixed-Term"}, ("Fixed-Term", "Temporary")),
    ({"employment": "", "time_type": ""}, ("", "Full-time")),
])
def test_to_job_employment_types(raw, want):
    p = {"ref": "1", "title": "Engineer", "locations": ["Austin, TX"], "html": "<p>Build things.</p>", **raw}
    job = sources.to_job(site("https://jobs.ashbyhq.com/umbrella"), "Umbrella", p)
    assert (job["worker_sub_type"], job["employment_type"]) == want


def test_to_job_lists_us_locations_first():
    p = ashby.posting(load("ashby_board.json")["jobs"][0])
    job = sources.to_job(site("https://jobs.ashbyhq.com/umbrella"), "Umbrella", p)
    assert job["location"] == "New York City" and job["locations_json"] == ["New York City", "London, UK"]
    assert job["states_json"] == ["NY"] and job["salary_text"] == "$200K - $250K"


# ---------------------------------------------------------------- a search over a mixed company list
class FakeBoards:
    """Serves tests/fixtures/boards/*.json for the four boards, and the Workday fixtures for Acme."""

    def __init__(self, gone=()):
        self.workday = FakeWorkday()
        self.calls = []
        self.gone = set(gone)

    def __call__(self, request):
        host, path = request.url.host, request.url.path
        self.calls.append((host, path, dict(request.url.params)))
        if host == "acme.wd1.myworkdayjobs.com":
            return self.workday(request)
        if host == "boards-api.greenhouse.io" and path == "/v1/boards/globex/jobs":
            assert request.url.params["content"] == "true"
            return httpx.Response(200, json=load("greenhouse_jobs.json"))
        if host == "api.lever.co" and path == "/v0/postings/hooli":
            return httpx.Response(200, json=load("lever_postings.json"))
        if host == "api.ashbyhq.com" and path == "/posting-api/job-board/umbrella":
            return httpx.Response(200, json=load("ashby_board.json"))
        if host == "api.smartrecruiters.com" and path == "/v1/companies/Initech/postings":
            assert request.url.params["country"] == "us"
            return httpx.Response(200, json=load("smartrecruiters_postings.json"))
        m = re.fullmatch(r"/v1/companies/Initech/postings/(\d+)", path)
        if host == "api.smartrecruiters.com" and m and m.group(1) not in self.gone:
            if (BOARDS / f"smartrecruiters_detail_{m.group(1)}.json").exists():
                return httpx.Response(200, json=load(f"smartrecruiters_detail_{m.group(1)}.json"))
        return httpx.Response(404, json={"message": "Not Found"})

    def detail_calls(self):
        return [p for h, p, _ in self.calls if h == "api.smartrecruiters.com" and p.count("/") == 5]


MIXED = f"""companies:
  - name: "Acme"
    url: {ACME_URL}
  - name: "Globex"
    url: https://job-boards.greenhouse.io/globex
  - name: "Hooli"
    url: https://jobs.lever.co/hooli
  - name: "Umbrella"
    url: https://umbrella.example.com/careers
    ats: ashby
    board: umbrella
  - name: "Initech"
    url: https://jobs.smartrecruiters.com/Initech
"""


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    path = tmp_path / "companies.yaml"
    path.write_text(MIXED, encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    fake = FakeBoards()
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(fake)))

    async def no_backoff(_):
        return None
    monkeypatch.setattr(workday.asyncio, "sleep", no_backoff)
    db.save_settings({"mandatory": "python"})
    db.save_resume("cv.txt", "Data engineer. Python, SQL, AWS, ETL pipelines.", SAMPLE_MASTER, 0)
    return fake


def _run(full_refresh=False):
    runner = search.SearchRunner()

    async def go():
        runner.start(full_refresh)
        await runner.task

    asyncio.run(go())
    return runner.state


EXPECTED = {
    "acme/external:R1", "globex/greenhouse:101", "globex/greenhouse:103",
    "hooli/lever:5f1c2a3b-0000-4000-8000-000000000001", "hooli/lever:5f1c2a3b-0000-4000-8000-000000000002",
    "umbrella/ashby:0a0b0c0d-0000-4000-8000-0000000000a1", "umbrella/ashby:0a0b0c0d-0000-4000-8000-0000000000a2",
    "initech/smartrecruiters:7440001",
}


def test_companies_list_shows_the_board(mixed):
    out = {c["name"]: c for c in search.load_companies()}
    assert {n: c["ats"] for n, c in out.items()} == {"Acme": "workday", "Globex": "greenhouse", "Hooli": "lever",
                                                     "Umbrella": "ashby", "Initech": "smartrecruiters"}
    assert out["Umbrella"]["key"] == "umbrella/ashby" and out["Umbrella"]["board"] == "umbrella"
    assert all(c["error"] is None for c in out.values())


def test_mixed_search_yields_jobs_from_every_source(mixed):
    st = _run()
    assert st["errors"] == [] and st["done"] == 5
    assert st["new_jobs"] == len(EXPECTED) and st["rejected"] == 2  # Acme R2, Initech 7440002: no "python"
    jobs = {j["id"]: db.get_job(j["id"]) for j in db.all_jobs()}
    assert set(jobs) == EXPECTED
    assert {j["source"] for j in jobs.values()} == {"workday", "greenhouse", "lever", "ashby", "smartrecruiters"}
    for j in jobs.values():  # consistent fields whatever the source
        assert j["id"].startswith(j["company_key"] + ":") and j["external_path"]
        if j["source"] != "workday":
            assert j["id"] == f"{j['company_key']}:{j['external_path']}"
        assert j["title"] and j["url"].startswith("https://") and j["posted_date"]
        assert j["employment_type"] in ("Full-time", "Internship", "Contract")
        assert j["remote_type"] in ("Remote", "Hybrid", "On-site", "Unspecified")
        assert j["match_score"] is not None and "Python" in j["matched"]
    lever_job = jobs["hooli/lever:5f1c2a3b-0000-4000-8000-000000000001"]
    assert (lever_job["company"], lever_job["remote_type"], lever_job["states"]) == ("Hooli", "On-site", ["TX"])
    assert (lever_job["salary_min"], lever_job["salary_max"]) == (124800, 156000)
    assert jobs["hooli/lever:5f1c2a3b-0000-4000-8000-000000000002"]["employment_type"] == "Internship"
    assert jobs["umbrella/ashby:0a0b0c0d-0000-4000-8000-0000000000a2"]["employment_type"] == "Contract"
    sr = db.get_job("initech/smartrecruiters:7440001")
    assert sr["url"] == "https://jobs.smartrecruiters.com/Initech/7440001-software-engineer-python"
    assert (sr["salary_min"], sr["salary_max"], sr["req_id"]) == (110000, 140000, "REF7441")
    assert sr["states"] == ["MI"] and sr["remote_type"] == "Hybrid"
    # SmartRecruiters was searched with the keywords; the other boards were read whole
    sr_lists = [q for h, p, q in mixed.calls if p == "/v1/companies/Initech/postings"]
    assert sr_lists and all(q.get("q") == "python" for q in sr_lists)
    assert sorted(mixed.detail_calls()) == ["/v1/companies/Initech/postings/7440001",
                                            "/v1/companies/Initech/postings/7440002"]


def test_incremental_run_makes_no_detail_requests(mixed):
    _run()
    details = len(mixed.detail_calls())
    st = _run()
    assert st["new_jobs"] == 0 and st["refreshed"] == 0 and st["errors"] == []
    assert len(mixed.detail_calls()) == details  # 7440001 is known, 7440002 is older than the 1-day window
    st = _run(full_refresh=True)
    assert st["refreshed"] == len(EXPECTED) and st["new_jobs"] == 0


def test_keyword_signature_change_is_a_new_search(mixed):
    _run()
    db.save_settings({"mandatory": "python, aws"})
    st = _run()
    # only Greenhouse 101 mentions AWS; it is stored already, so nothing new and no Workday detail repeat for R1
    assert st["new_jobs"] == 0 and st["errors"] == []


def test_closed_postings_on_job_boards(mixed, tmp_path, monkeypatch):
    tracked = {
        "globex/greenhouse:999": ("applied", "999"),          # no longer on the board
        "globex/greenhouse:101": ("interviewing", "101"),     # still on the board (even though it's listed again)
        "hooli/lever:gone-1": ("saved", "gone-1"),
        "initech/smartrecruiters:7449999": ("applied", "7449999"),  # SmartRecruiters: a 404 on the posting
    }
    for job_id, (status, ref) in tracked.items():
        key = job_id.split(":")[0]
        db.upsert_job(make_job(job_id, company_key=key, tenant=key.split("/")[0], site=key.split("/")[1],
                               source=key.split("/")[1], external_path=ref, status=status))
    st = _run()
    assert st["errors"] == []
    closed = {j: bool(db.get_job(j)["closed_at"]) for j in tracked}
    assert closed == {"globex/greenhouse:999": True, "globex/greenhouse:101": False, "hooli/lever:gone-1": True,
                      "initech/smartrecruiters:7449999": True}
    assert db.get_job("globex/greenhouse:999")["status"] == "applied"  # closed, never deleted
    assert db.job_events("globex/greenhouse:999")[-1]["kind"] == "closed"
    assert db.get_job("globex/greenhouse:101")["checked_at"]


def test_board_failure_is_reported_and_closes_nothing(mixed, monkeypatch):
    db.upsert_job(make_job("globex/greenhouse:999", company_key="globex/greenhouse", tenant="globex",
                           site="greenhouse", source="greenhouse", external_path="999", status="applied"))
    real = mixed.__call__

    def flaky(request):
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(503)
        return real(request)
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(flaky)))
    st = _run()
    assert [e.split(":")[0] for e in st["errors"]] == ["Globex"]
    assert db.get_job("globex/greenhouse:999")["closed_at"] is None
    assert st["done"] == 5 and st["new_jobs"] == len(EXPECTED) - 2


def test_backfilled_board_job_keeps_its_source(tmp_path):
    job = pipeline._job_from_meta("globex/greenhouse:101", {"company": "Globex", "title": "Data Engineer"},
                                  tmp_path, "applied")
    assert (job["source"], job["tenant"], job["site"]) == ("greenhouse", "globex", "greenhouse")
    assert pipeline._job_from_meta("acme/external:R1", {}, tmp_path, "applied")["source"] == "workday"
