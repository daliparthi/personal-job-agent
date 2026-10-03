"""The application pipeline: statuses, timeline, follow-ups, board, statistics, recovery from disk, closed postings."""
import asyncio
import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from app import apply, config, db, pipeline, search, workday
from app.workday import WorkdayClient
from tests.conftest import SAMPLE_MASTER, make_job

TODAY = date.today()


def _ago(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def _job(job_id="acme/external:R1", status=None, status_days_ago=0, **over):
    db.upsert_job(make_job(job_id, **over))
    if status:
        db.set_status(job_id, status, _ago(status_days_ago))
        db.add_event(job_id, "status", to_status=status, at=_ago(status_days_ago))
    return job_id


# ---------------------------------------------------------------- statuses and the timeline
def test_automatic_moves_only_go_forward():
    j = _job()
    assert pipeline.advance(j, "tailored", "tailored resume saved")
    assert pipeline.advance(j, "applied", "marked as applied")
    assert not pipeline.advance(j, "saved", "application package saved")  # re-packaging after applying
    pipeline.set_status(j, "interviewing", note="phone screen went well")
    assert not pipeline.advance(j, "applied", "marked as applied")
    assert db.get_job(j)["status"] == "interviewing"
    events = db.job_events(j)
    assert [(e["from_status"], e["to_status"]) for e in events] == [("new", "tailored"), ("tailored", "applied"),
                                                                    ("applied", "interviewing")]
    assert events[-1]["note"] == "phone screen went well"


def test_manual_moves_go_anywhere_and_validate():
    j = _job(status="interviewing")
    assert pipeline.set_status(j, "applied")  # undo a mistaken move
    assert not pipeline.set_status(j, "applied")  # no change, no event
    with pytest.raises(ValueError):
        pipeline.set_status(j, "hired")
    with pytest.raises(KeyError):
        pipeline.set_status("nope:1", "applied")


def test_notes_and_follow_ups():
    j = _job()
    pipeline.add_note(j, "  Recruiter: Sam, sam@example.com  ")
    with pytest.raises(ValueError):
        pipeline.add_note(j, "   ")
    pipeline.set_follow_up(j, TODAY + timedelta(days=3), "Email Sam")
    job = db.get_job(j)
    assert job["next_action_at"] == (TODAY + timedelta(days=3)).isoformat() and job["next_action"] == "Email Sam"
    pipeline.set_follow_up(j, None)
    assert db.get_job(j)["next_action_at"] is None
    with pytest.raises(ValueError):
        pipeline.set_follow_up(j, "next tuesday")
    kinds = [e["kind"] for e in db.job_events(j)]
    assert kinds == ["note", "follow_up", "follow_up"]
    assert db.job_events(j)[0]["note"] == "Recruiter: Sam, sam@example.com"


def test_jobs_with_notes_or_follow_ups_are_never_purged():
    old = (TODAY - timedelta(days=60)).isoformat()
    _job("a:noted", posted_date=old)
    pipeline.add_note("a:noted", "keep me")
    _job("a:follow", posted_date=old)
    db.set_follow_up("a:follow", TODAY.isoformat(), "call")
    _job("a:plain", posted_date=old)
    assert db.purge_old() == 1
    assert db.get_job("a:noted") and db.get_job("a:follow") and not db.get_job("a:plain")


def test_purge_window_is_configurable():
    _job("a:20", posted_date=(TODAY - timedelta(days=20)).isoformat())
    assert db.purge_old(30) == 0 and db.purge_old(10) == 1


# ---------------------------------------------------------------- application.json stays in sync
def _fake_pdf(monkeypatch):
    async def render(html, out_path):
        out_path.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(apply.worker, "render_pdf", render)


def _package(job_id):
    r = asyncio.run(apply.save_package(db.get_job(job_id), SAMPLE_MASTER, 40, 55, [], [], {}))
    db.update_job(job_id, folder=r["folder"])
    return json_path(r["folder"])


def json_path(folder):
    from pathlib import Path
    return Path(folder) / "application.json"


def test_status_and_timeline_are_mirrored_into_application_json(monkeypatch):
    _fake_pdf(monkeypatch)
    j = _job()
    path = _package(j)
    apply.mark_applied(j, db.get_job(j)["folder"], how="manual")
    pipeline.set_status(j, "interviewing")
    pipeline.add_note(j, "onsite on Friday")
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["status"] == "interviewing" and meta["applied_how"] == "manual" and meta["applied_at"]
    assert [e["kind"] for e in meta["history"]] == ["status", "status", "note"]
    _package(j)  # re-packaging an interviewing job keeps its status in the file
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "interviewing"


def test_mark_applied_never_moves_back(monkeypatch):
    _fake_pdf(monkeypatch)
    j = _job(status="offer")
    _package(j)
    apply.mark_applied(j, db.get_job(j)["folder"], how="detected")
    assert db.get_job(j)["status"] == "offer"


# ---------------------------------------------------------------- the board
def test_board_columns_follow_ups_and_ghosting():
    _job("a:prep", status="tailored")
    _job("a:applied-old", status="applied", status_days_ago=30)
    _job("a:applied-new", status="applied", status_days_ago=2)
    _job("a:rej", status="rejected", status_days_ago=40)
    _job("a:new")  # not on the board
    db.set_follow_up("a:applied-new", (TODAY - timedelta(days=1)).isoformat(), "Nudge")
    b = pipeline.board(ghost_after_days=21)
    cols = {c["key"]: [j["id"] for j in c["jobs"]] for c in b["columns"]}
    assert cols["preparing"] == ["a:prep"] and cols["rejected"] == ["a:rej"]
    assert cols["applied"] == ["a:applied-new", "a:applied-old"]  # most recent change first
    jobs = {j["id"]: j for c in b["columns"] for j in c["jobs"]}
    assert jobs["a:applied-old"]["suggest_ghosted"] and not jobs["a:applied-new"]["suggest_ghosted"]
    assert not jobs["a:rej"]["suggest_ghosted"] and jobs["a:applied-old"]["days_in_status"] == 30
    assert [j["id"] for j in b["due"]] == ["a:applied-new"]
    assert b["labels"]["screening"] == "Recruiter screen"
    assert [c["drop_status"] for c in b["columns"]][0] == "saved"


def test_old_applied_jobs_leave_the_search_list_but_stay_on_the_board():
    old = (TODAY - timedelta(days=30)).isoformat()
    _job("a:applied-old", status="applied", posted_date=old)
    _job("a:tailored-old", status="tailored", posted_date=old)
    _job("a:applied-recent", status="applied")
    listed = [j["id"] for j in search.list_jobs(db.get_settings())]
    assert sorted(listed) == ["a:applied-recent", "a:tailored-old"]
    on_board = {j["id"] for c in pipeline.board()["columns"] for j in c["jobs"]}
    assert on_board == {"a:applied-old", "a:tailored-old", "a:applied-recent"}


# ---------------------------------------------------------------- statistics
def test_stats():
    # applied this week with a high score -> interviewing; applied last week -> rejected; one ghosted; one still
    # being prepared; one moved straight to interviewing on the board; one withdrawn before applying.
    _job("a:1", match_score=85, status="applied", status_days_ago=0)
    pipeline.set_status("a:1", "interviewing")
    _job("a:2", match_score=45, status="applied", status_days_ago=8)
    pipeline.set_status("a:2", "rejected")
    _job("a:3", match_score=65, status="applied", status_days_ago=9)
    pipeline.set_status("a:3", "ghosted")
    _job("a:4", match_score=90, status="saved")
    _job("a:5", match_score=None)
    pipeline.set_status("a:5", "interviewing")
    _job("a:6", status="saved")
    pipeline.set_status("a:6", "withdrawn")
    db.save_tailored("a:2", {"resume": {}}, 72, [], [])  # the tailored score counts when there is one

    s = pipeline.stats(weeks=4)
    assert (s["applied"], s["responses"], s["interviews"], s["offers"]) == (4, 3, 2, 0)
    assert s["response_rate"] == 75 and s["interview_rate"] == 50 and s["waiting"] == 2
    assert len(s["per_week"]) == 4 and sum(w["applied"] for w in s["per_week"]) == 4
    assert s["per_week"][-1]["applied"] >= 2  # a:1 and a:5 this week
    buckets = {b["bucket"]: b for b in s["by_score"]}
    assert buckets["80+"] == {"bucket": "80+", "applied": 1, "interviews": 1, "rate": 100}
    assert buckets["60–79"]["applied"] == 2 and buckets["60–79"]["interviews"] == 0  # a:3 (65), a:2 (tailored 72)
    assert buckets["40–59"]["applied"] == 0 and s["unscored"] == 1


def test_stats_with_nothing_tracked():
    s = pipeline.stats()
    assert s["applied"] == 0 and s["response_rate"] is None and all(b["rate"] is None for b in s["by_score"])


# ---------------------------------------------------------------- recovery from the Applications folder
def _write_meta(company, title, meta):
    folder = config.APPLICATIONS / company / title
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "application.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "job_description.txt").write_text(f"{title}\n{company}\nhttps://x\n\nWe use Python and SQL.",
                                                encoding="utf-8")
    return folder


def test_backfill_restores_jobs_an_old_version_purged():
    folder = _write_meta("Acme", "Data Engineer - R9", {
        "job_id": "acme/external:R9", "company": "Acme", "title": "Data Engineer", "req_id": "R9",
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x_R9", "location": ["Austin, TX"],
        "employment_type": "Full-time", "work_mode": "Hybrid", "salary_min": 100000, "salary_max": 140000,
        "posted_date": "2025-06-01", "match_score_before": 61, "status": "applied",
        "prepared_at": "2025-06-02T10:00:00", "applied_at": "2025-06-03T09:00:00"})
    _write_meta("Globex", "Analyst - R2", {"job_id": "globex/careers:R2", "company": "Globex", "title": "Analyst",
                                           "status": "prepared", "prepared_at": "2025-07-01T10:00:00"})
    _write_meta("Broken", "x", {"no": "job id"})
    _job("initech/jobs:R3", status="saved")
    _write_meta("Initech", "Engineer - R3", {"job_id": "initech/jobs:R3", "status": "interviewing"})

    assert pipeline.backfill_from_disk() == 2
    j = db.get_job("acme/external:R9")
    assert j["status"] == "applied" and j["folder"] == str(folder) and j["states"] == ["TX"]
    assert j["description_text"] == "We use Python and SQL." and j["match_score"] == 61
    assert db.job_events("acme/external:R9")[0]["at"] == "2025-06-03T09:00:00"
    assert db.get_job("globex/careers:R2")["status"] == "saved"
    assert db.get_job("initech/jobs:R3")["status"] == "interviewing"  # the file knew a later status
    assert pipeline.backfill_from_disk() == 0  # idempotent
    assert db.purge_old() == 0  # restored jobs are kept
    assert pipeline.stats()["applied"] == 2


def test_legacy_database_gets_a_timeline():
    from tests.test_storage import _legacy_db
    _legacy_db()
    db.init()
    events = db.job_events("acme/external:OLD1")
    assert events and events[0]["to_status"] == "applied" and "before the pipeline tracker" in events[0]["note"]
    assert db.get_job("acme/external:OLD1")["status_at"]


# ---------------------------------------------------------------- closed postings, checked during searches
class Site:
    def __init__(self, detail_status):
        self.detail_status = detail_status
        self.detail_paths = []

    def __call__(self, request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 0, "facets": [], "jobPostings": []})
        self.detail_paths.append(request.url.path)
        code = self.detail_status.get(request.url.path.rsplit("/", 1)[-1], 200)
        return httpx.Response(code, json={"jobPostingInfo": {"title": "x"}} if code == 200 else {"errorCode": "S21"})


def _search_with(site, tmp_path, monkeypatch):
    async def no_backoff(_):
        return None
    monkeypatch.setattr(workday.asyncio, "sleep", no_backoff)  # a 503 is retried with backoff; don't wait in tests
    path = tmp_path / "companies.yaml"
    path.write_text('companies:\n  - name: "Acme"\n    url: https://acme.wd1.myworkdayjobs.com/External\n',
                    encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(site)))
    db.save_settings({"mandatory": "python"})
    runner = search.SearchRunner()

    async def go():
        runner.start(False)
        await runner.task

    asyncio.run(go())
    return runner.state


def test_search_marks_tracked_postings_that_closed(tmp_path, monkeypatch):
    for ref, status in (("GONE", "applied"), ("OPEN", "interviewing"), ("FLAKY", "saved"), ("DONE", "rejected")):
        _job(f"acme/external:{ref}", status=status, company_key="acme/external", external_path=f"/job/x/{ref}")
    site = Site({"GONE": 404, "FLAKY": 503})
    st = _search_with(site, tmp_path, monkeypatch)
    assert any("no longer posted" in line for line in st["log"])
    gone, open_, flaky = (db.get_job(f"acme/external:{r}") for r in ("GONE", "OPEN", "FLAKY"))
    assert gone["closed_at"] and gone["status"] == "applied"  # closed, but the application stays where it was
    assert db.job_events("acme/external:GONE")[-1]["kind"] == "closed"
    assert open_["closed_at"] is None and open_["checked_at"]
    assert flaky["closed_at"] is None and flaky["checked_at"] is None  # a 503 isn't a closed posting
    assert not any(p.endswith("/DONE") for p in site.detail_paths)  # outcomes are not checked
    n = len(site.detail_paths)
    _search_with(site, tmp_path, monkeypatch)
    assert {p.rsplit("/", 1)[-1] for p in site.detail_paths[n:]} == {"FLAKY"}  # the others were checked today
