import asyncio

import httpx
import pytest

from app import db, discovery, search
from app.workday import WorkdayClient
from tests.test_search import ACME_URL, FakeWorkday
from tests.conftest import SAMPLE_MASTER

RESULTS = """
<a href="/url?q=https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/SQL-Dev_JR1&amp;sa=U">x</a>
<a href="/url?q=https%3A%2F%2Facme.wd1.myworkdayjobs.com%2FExternal%2Fjob%2FAustin-TX%2FData-Engineer_R1&amp;sa=U">y</a>
<a href="https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Analyst_R9">dup of acme</a>
<a href="https://wd3.myworkdaysite.com/recruiting/globex/Careers/job/NY/Dev_1">z</a>
<a href="https://example.com/not-workday">no</a>
"""


def test_build_query():
    assert discovery.build_query(["sql"]) == 'site:myworkdayjobs.com "sql"'
    assert discovery.build_query(["sql", "power bi"]) == 'site:myworkdayjobs.com "sql" "power bi"'


def test_sites_from_text_dedupes_by_career_site():
    keys = [s.key for s in discovery.sites_from_text(RESULTS)]
    assert keys == ["nvidia/nvidiaexternalcareersite", "acme/external", "globex/careers"]


def _google(handler):
    return httpx.MockTransport(handler)


def test_find_sites_pages_until_results_run_out():
    pages = []

    def handler(request):
        pages.append(request.url.params["start"])
        assert request.url.params["q"] == 'site:myworkdayjobs.com "sql"'
        return httpx.Response(200, text=RESULTS if request.url.params["start"] == "0" else "<html></html>")

    sites = asyncio.run(discovery.find_workday_sites(["sql"], delay=0, transport=_google(handler)))
    assert [s.tenant for s in sites] == ["nvidia", "acme", "globex"]
    assert sites[0].name == "Nvidia" and sites[1].url == "https://acme.wd1.myworkdayjobs.com/External"
    assert pages == ["0", "10"]


def test_blocked_by_google_raises():
    def handler(request):
        return httpx.Response(429, text="unusual traffic")

    with pytest.raises(discovery.DiscoveryError):
        asyncio.run(discovery.find_workday_sites(["sql"], delay=0, transport=_google(handler)))


def test_javascript_only_page_is_an_error_not_an_empty_result():
    def handler(request):
        return httpx.Response(200, text="<a>Please click here if you are not redirected within a few seconds.</a>")

    with pytest.raises(discovery.DiscoveryError, match="GOOGLE_API_KEY"):
        asyncio.run(discovery.find_workday_sites(["sql"], delay=0, transport=_google(handler)))


def test_search_api_is_used_when_keys_are_set():
    from app import config
    config.ENV_FILE.write_text("GOOGLE_API_KEY=k" + chr(10) + "GOOGLE_CSE_ID=cx" + chr(10), encoding="utf-8")
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        items = [{"link": "https://acme.wd1.myworkdayjobs.com/en-US/External/job/X/Y_R1"}] if len(seen) == 1 else []
        return httpx.Response(200, json={"items": items})

    sites = asyncio.run(discovery.find_workday_sites(["sql"], delay=0, transport=_google(handler)))
    assert [s.key for s in sites] == ["acme/external"]
    assert seen[0]["key"] == "k" and seen[0]["cx"] == "cx" and seen[0]["q"] == 'site:myworkdayjobs.com "sql"'


# ---------------------------------------------------------------- a search that uses it
@pytest.fixture
def google_search(tmp_path, monkeypatch):
    """companies.yaml lists Acme (Workday) and a Lever board; Google finds only Acme's career site."""
    path = tmp_path / "companies.yaml"
    path.write_text(f'companies:\n  - name: "Acme"\n    url: {ACME_URL}\n'
                    '  - name: "Old Workday"\n    url: https://old.wd1.myworkdayjobs.com/Jobs\n'
                    '  - name: "Initech"\n    url: https://jobs.lever.co/initech\n', encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    db.save_settings({"mandatory": "sql"})
    db.save_resume("cv.txt", "Python SQL", SAMPLE_MASTER, 0)


def _plan(found):
    async def fake(keywords, **kw):
        return found
    return fake


def _discover(monkeypatch, found):
    monkeypatch.setattr(discovery, "find_workday_sites", _plan(found))
    runner = search.SearchRunner()
    listed = [c for c in search.load_companies()]
    return asyncio.run(runner._discover(["sql"])), listed, runner


def test_google_sites_replace_the_workday_entries(google_search, monkeypatch):
    from app.workday import parse_site
    found, listed, _ = _discover(monkeypatch, [parse_site("Acme Corp", ACME_URL),
                                               parse_site("Newco", "https://newco.wd5.myworkdayjobs.com/Careers")])
    names = [c["name"] for c in search.plan_companies(listed, found)]
    # the Lever board stays; Acme keeps its companies.yaml name; "Old Workday" is not searched; Newco is new
    assert names == ["Initech", "Acme", "Newco"]


def test_companies_yaml_is_the_fallback(google_search, monkeypatch):
    found, listed, runner = _discover(monkeypatch, [])
    assert found is None and any("using the Workday companies" in line for line in runner.state["log"])
    assert [c["name"] for c in search.plan_companies(listed, found)] == ["Initech", "Acme", "Old Workday"]


def test_google_failure_is_reported_and_falls_back(google_search, monkeypatch):
    async def blocked(keywords, **kw):
        raise discovery.DiscoveryError("blocked")
    monkeypatch.setattr(discovery, "find_workday_sites", blocked)
    runner = search.SearchRunner()
    assert asyncio.run(runner._discover(["sql"])) is None
    assert runner.state["errors"] == ["Google discovery failed: blocked"]


def test_no_mandatory_keywords_skips_google(google_search, monkeypatch):
    calls = []

    async def spy(keywords, **kw):
        calls.append(keywords)
        return []
    monkeypatch.setattr(discovery, "find_workday_sites", spy)
    assert asyncio.run(search.SearchRunner()._discover([])) is None and calls == []


def test_whole_search_searches_the_google_site(google_search, monkeypatch):
    from app.workday import parse_site
    fake = FakeWorkday()
    monkeypatch.setattr(discovery, "find_workday_sites", _plan([parse_site("", ACME_URL)]))
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(search.sources.BOARDS["lever"], "postings", _no_postings)
    runner = search.SearchRunner()

    async def go():
        runner.start(False)
        await runner.task
    asyncio.run(go())
    assert runner.state["total"] == 2 and fake.list_bodies  # Acme (via Google) + Initech; "Old Workday" skipped


async def _no_postings(client, site, query):
    return []
