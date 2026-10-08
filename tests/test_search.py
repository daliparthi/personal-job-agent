import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest
import yaml

from app import config, db, search
from app.workday import WorkdayClient
from tests.conftest import FIXTURES, SAMPLE_MASTER, make_job

WD = FIXTURES / "workday"
ACME_URL = "https://acme.wd1.myworkdayjobs.com/External"


# ---------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("company, key, employer, aliases, want", [
    ("NVIDIA", "nvidia/nvidiaexternalcareersite", "Nvidia Corp", (), True),
    ("Intel", "intel/external", "Salesforce", (), False),
    ("Alphabet", "google/x", "Google", (), True),          # matches the Workday tenant
    ("Meta", "meta/x", "Facebook", ("Facebook",), True),   # matches an alias
    ("Acme", "acme/x", "", (), False),
    ("Acme", "acme/x", "AC", (), False),                   # too short to mean anything
])
def test_is_current_employer(company, key, employer, aliases, want):
    assert search.is_current_employer(company, key, employer, aliases) is want


def test_keyword_signature():
    sig = search.keyword_signature
    assert sig(["Python", "sql"], []) == sig(["SQL", "python"], ["anything"])  # optional ignored with mandatory
    assert sig([], ["spark"]) != sig(["spark"], [])
    assert len(sig([], [])) == 12
    assert sig(["python"], [], []) == sig(["python"], [])           # no omit words: the same search as before
    assert sig(["python"], [], ["senior"]) != sig(["python"], [])   # omit words change which postings are kept


# ---------------------------------------------------------------- company lists
@pytest.fixture
def shared(tmp_path, monkeypatch):
    path = tmp_path / "companies.yaml"
    path.write_text(f'companies:\n  - name: "Acme"\n    url: {ACME_URL}\n'
                    '  - name: "Globex"\n    url: https://globex.wd5.myworkdayjobs.com/Careers\n'
                    '    aliases: Globex Corporation\n'
                    '  - name: "Broken"\n    url: https://example.com/jobs\n', encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    return path


def test_load_companies_merges_your_list(shared):
    search.append_company('Globex "Main"', "https://globex.wd5.myworkdayjobs.com/Careers", enabled=False)
    search.append_company("Initech", "https://initech.wd1.myworkdayjobs.com/Jobs", aliases=["Initrode"])
    out = search.load_companies()
    assert [c["name"] for c in out] == ["Acme", "Globex 'Main'", "Broken", "Initech"]
    globex = out[1]
    assert globex["source"] == "yours" and globex["enabled"] is False  # your entry replaced the shared one in place
    assert out[2]["key"] is None and "Not a supported job board URL" in out[2]["error"]
    assert out[3]["aliases"] == ["Initrode"]
    assert config.MY_COMPANIES.read_text(encoding="utf-8").startswith("# my_companies.yaml")


def test_append_company_validates_url(shared):
    with pytest.raises(ValueError):
        search.append_company("Nope", "https://careers.example.com/nope")
    assert not config.MY_COMPANIES.exists()


def test_yaml_error_is_reported_not_raised(shared):
    shared.write_text("companies: [\n", encoding="utf-8")
    out = search.load_companies()
    assert len(out) == 1 and out[0]["error"].startswith("YAML error")


def test_old_personal_copy_is_migrated(shared):
    config.OLD_COMPANIES_COPY.write_text(f'companies:\n  - name: "Acme"\n    url: {ACME_URL}\n'
                                         '  - name: "Mine"\n    url: https://mine.wd1.myworkdayjobs.com/X\n',
                                         encoding="utf-8")
    names = [c["name"] for c in search.load_companies()]
    assert names.count("Acme") == 1 and "Mine" in names
    assert not config.OLD_COMPANIES_COPY.exists()
    assert (config.HOME / "companies.yaml.old").exists()


@pytest.fixture
def by_industry(tmp_path, monkeypatch):
    """A companies folder with one file per industry, like the program's own."""
    folder = tmp_path / "companies"
    folder.mkdir()
    (folder / "healthcare.yaml").write_text(
        f'industry: "Healthcare & Life Sciences"\ncompanies:\n  - name: "Acme"\n    url: {ACME_URL}\n', encoding="utf-8")
    (folder / "aerospace_defense.yaml").write_text(  # no industry line: named after the file
        'companies:\n  - name: "Globex"\n    url: https://globex.wd5.myworkdayjobs.com/Careers\n'
        '  - name: "Initech"\n    url: https://initech.wd1.myworkdayjobs.com/Jobs\n    industry: AI\n', encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", folder)
    return folder


def test_companies_come_from_one_file_per_industry(by_industry):
    search.append_company("Acme again", ACME_URL, enabled=False)           # overrides the shared Acme
    search.append_company("Hooli", "https://hooli.wd1.myworkdayjobs.com/X")  # no industry: Other
    search.append_company("Umbrella", "https://umbrella.wd1.myworkdayjobs.com/X", industry="Healthcare & Life Sciences")
    got = {c["name"]: c["industry"] for c in search.load_companies()}
    assert got == {"Globex": "Aerospace Defense", "Initech": "AI", "Acme again": "Healthcare & Life Sciences",
                   "Hooli": "Other", "Umbrella": "Healthcare & Life Sciences"}
    assert search.industries() == [{"name": "Aerospace Defense", "companies": 1}, {"name": "AI", "companies": 1},
                                   {"name": "Healthcare & Life Sciences", "companies": 2}, {"name": "Other", "companies": 1}]
    assert 'industry: "Healthcare & Life Sciences"' in config.MY_COMPANIES.read_text(encoding="utf-8")


def test_the_program_lists_every_company_once_under_an_industry():
    files = search.shared_files(config.COMPANIES_SHARED)
    assert len(files) >= 10 and config.COMPANIES_SHARED.is_dir()
    urls = []
    for p in files:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert str(doc.get("industry") or "").strip(), p.name
        urls += [c["url"] for c in doc["companies"]]
    assert len(urls) == len(set(urls)) and len(urls) > 200


def test_picked_industries_limit_the_search_and_the_list(fake_site, by_industry):
    db.save_settings({"industries": ["AI"]})
    st = _run()
    assert st["total"] == 1 and any("Industries: AI" in line for line in st["log"])  # only Initech
    assert not any(b for b in fake_site.list_bodies)  # Acme (healthcare) was not searched
    db.save_settings({"industries": ["Healthcare & Life Sciences"]})
    st = _run()
    assert st["total"] == 1 and db.get_job("acme/external:R1")
    db.upsert_job(make_job("gone/x:1", company_key="gone/x"))  # a company no longer in any list stays listed
    listed = lambda **s: {j["id"] for j in search.list_jobs(_settings(**s))}  # noqa: E731
    assert listed(industries=["Healthcare & Life Sciences"]) == {"acme/external:R1", "gone/x:1"}
    assert listed(industries=["AI"]) == {"gone/x:1"}
    assert listed(industries=[]) == {"acme/external:R1", "gone/x:1"}


# ---------------------------------------------------------------- the job list view
def _settings(**over):
    s = db.get_settings()
    for k, v in over.items():
        if isinstance(v, dict):
            s[k].update(v)
        else:
            s[k] = v
    return s


def test_list_jobs_filters_and_sorting():
    db.upsert_job(make_job("a:1", match_score=40, salary_max=100000.0))
    db.upsert_job(make_job("a:2", match_score=80, remote_type="Remote", states_json=[], locations_json=["Remote"]))
    db.upsert_job(make_job("a:3", match_score=None, employment_type="Contract", salary_max=None, salary_min=None))
    db.upsert_job(make_job("a:4", match_score=90, title="Accountant", description_text="Excel and close"))
    db.upsert_job(make_job("a:5", match_score=60, states_json=["WA"], locations_json=["Seattle, WA"]))
    db.update_job("a:5", hidden=1)

    ids = lambda s: [j["id"] for j in search.list_jobs(s)]  # noqa: E731
    assert ids(_settings()) == ["a:4", "a:2", "a:1", "a:3"]
    assert ids(_settings(mandatory="python")) == ["a:2", "a:1", "a:3"]
    assert ids(_settings(filters={"show_hidden": True})) == ["a:4", "a:2", "a:5", "a:1", "a:3"]
    assert ids(_settings(filters={"remote_only": True})) == ["a:2"]
    assert ids(_settings(filters={"types": ["Contract"]})) == ["a:3"]
    assert ids(_settings(filters={"min_salary": 120000})) == ["a:4", "a:2", "a:3"]
    assert ids(_settings(filters={"min_salary": 120000, "include_no_salary": False})) == ["a:4", "a:2"]
    assert ids(_settings(filters={"states": ["TX"]})) == ["a:4", "a:2", "a:1", "a:3"]  # remote kept
    assert ids(_settings(filters={"states": ["WA"], "show_hidden": True})) == ["a:2", "a:5"]
    assert ids(_settings(filters={"city": "seattle", "show_hidden": True})) == ["a:5"]
    assert ids(_settings(optional="sql, aws", filters={"require_optional": True})) == ["a:2", "a:1", "a:3"]
    assert ids(_settings(current_employer="Acme")) == []
    job = next(j for j in search.list_jobs(_settings(optional="SQL, kafka")) if j["id"] == "a:1")
    assert job["optional_hits"] == ["SQL"] and "description_text" not in job


def test_rescore_all_uses_the_master_resume():
    db.upsert_job(make_job("a:1"))
    assert search.rescore_all() == 1
    assert db.get_job("a:1")["match_score"] is None
    db.save_resume("cv.txt", "Python SQL AWS", SAMPLE_MASTER, 0)
    search.rescore_all()
    j = db.get_job("a:1")
    assert j["match_score"] > 0 and {"Python", "SQL", "AWS"} <= set(j["matched"])


# ---------------------------------------------------------------- a whole search against recorded Workday JSON
class FakeWorkday:
    """Serves tests/fixtures/workday/*.json in place of a live Workday career site."""

    def __init__(self):
        self.list_bodies = []
        self.details = []

    def __call__(self, request):
        assert request.url.host == "acme.wd1.myworkdayjobs.com"
        if request.method == "POST" and request.url.path == "/wday/cxs/acme/External/jobs":
            body = json.loads(request.content)
            self.list_bodies.append(body)
            data = json.loads((WD / "list.json").read_text(encoding="utf-8"))
            if body["offset"] >= data["total"]:
                data["jobPostings"] = []
            return httpx.Response(200, json=data)
        ref = request.url.path.rsplit("_", 1)[-1]
        self.details.append(ref)
        return httpx.Response(200, json=json.loads((WD / f"detail_{ref}.json").read_text(encoding="utf-8")))


@pytest.fixture
def fake_site(tmp_path, monkeypatch):
    path = tmp_path / "companies.yaml"
    path.write_text(f'companies:\n  - name: "Acme"\n    url: {ACME_URL}\n', encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    fake = FakeWorkday()
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(fake)))
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


def test_search_end_to_end(fake_site):
    st = _run()
    assert st["errors"] == [] and st["new_jobs"] == 1 and st["rejected"] == 1 and st["done"] == 1
    j = db.get_job("acme/external:R1")
    assert j["title"] == "Data Engineer" and j["company"] == "Acme" and j["req_id"] == "R1"
    assert j["url"] == "https://acme.wd1.myworkdayjobs.com/External/job/Austin-TX/Data-Engineer_R1"
    assert (j["salary_min"], j["salary_max"]) == (120000, 150000)
    assert j["remote_type"] == "Hybrid" and j["employment_type"] == "Full-time" and j["worker_sub_type"] == "Regular"
    assert j["states"] == ["TX"] and j["locations"] == ["Austin, TX", "Remote, US"]
    assert j["match_score"] > 0 and "Python" in j["matched"]
    assert db.get_job("acme/external:R2") is None  # no "python": rejected, never stored
    # the US country facet and then the job-type facet were applied
    assert fake_site.list_bodies[0]["appliedFacets"] == {}
    assert fake_site.list_bodies[1]["appliedFacets"] == {"locationCountry": ["us1"]}
    assert fake_site.list_bodies[2]["appliedFacets"] == {"locationCountry": ["us1"], "workerSubType": ["reg"]}
    assert all(b["searchText"] == "python" for b in fake_site.list_bodies)
    assert sorted(fake_site.details) == ["R1", "R2"]


def test_incremental_run_skips_known_and_old_postings(fake_site):
    _run()
    calls = len(fake_site.details)
    st = _run()
    assert st["new_jobs"] == 0 and st["refreshed"] == 0
    assert len(fake_site.details) == calls  # R1 is known, R2 is older than the 1-day incremental window
    st = _run(full_refresh=True)
    assert st["refreshed"] == 1 and st["rejected"] == 1
    assert len(fake_site.details) == calls + 2


def test_current_employer_is_skipped(fake_site):
    db.save_settings({"current_employer": "Acme Inc"})
    st = _run()
    assert st["total"] == 0 and fake_site.list_bodies == []
    assert any("Skipping Acme" in line for line in st["log"])


def test_search_needs_a_keyword(fake_site):
    db.save_settings({"mandatory": "", "optional": ""})
    st = _run()
    assert any("Enter at least one" in line for line in st["log"]) and fake_site.list_bodies == []


def test_one_failing_company_does_not_stop_the_run(fake_site, monkeypatch):
    def broken(request):
        return httpx.Response(400)
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(broken)))
    st = _run()
    assert st["running"] is False and st["done"] == 1 and len(st["errors"]) == 1 and st["errors"][0].startswith("Acme")


def test_list_jobs_sort_options():
    day = lambda n: (date.today() - timedelta(days=n)).isoformat()  # noqa: E731
    db.upsert_job(make_job("s:1", match_score=90, salary_max=100000.0, posted_date=day(2)))
    db.upsert_job(make_job("s:2", match_score=50, salary_max=200000.0, posted_date=day(1)))
    db.upsert_job(make_job("s:3", match_score=70, salary_max=None, salary_min=None, posted_date=day(1)))
    ids = lambda sort: [j["id"] for j in search.list_jobs(_settings(filters={"sort": sort}))]  # noqa: E731
    assert ids("match_salary") == ["s:1", "s:3", "s:2"]
    assert ids("date_salary") == ["s:2", "s:3", "s:1"]
    assert ids("date_match") == ["s:3", "s:2", "s:1"]
    assert ids("salary_date") == ["s:2", "s:1", "s:3"]
    assert ids("date_oldest") == ["s:1", "s:3", "s:2"]
    assert ids("nonsense") == ids("match_salary")  # unknown values fall back to the default
