"""Saved searches, the scheduler, the run history, new-match alerts and the headless run.py modes."""
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import alerts, config, db, main, scheduler, search
from app.workday import WorkdayClient
from tests.conftest import ROOT, SAMPLE_MASTER, make_job
from tests.test_search import ACME_URL, FakeWorkday


def _ago(hours):
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def _saved(name="Data", mandatory="python", optional="", every_hours=None, notify=None, enabled=1, last_run_at=None):
    sid = db.create_saved_search({"name": name, "mandatory": mandatory, "optional": optional, "every_hours": every_hours,
                                  "notify_min_score": notify, "enabled": enabled})
    if last_run_at:
        db.mark_search_ran(sid, last_run_at)
    return sid


@pytest.fixture
def fake_site(tmp_path, monkeypatch):
    path = tmp_path / "companies.yaml"
    path.write_text(f'companies:\n  - name: "Acme"\n    url: {ACME_URL}\n', encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    fake = FakeWorkday()
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(fake)))
    db.save_resume("cv.txt", "Data engineer. Python, SQL, AWS, ETL pipelines, Spark.", SAMPLE_MASTER, 0)
    return fake


# ---------------------------------------------------------------- the schedule
def test_next_run_and_due_order():
    never = _saved("never-run", every_hours=6)
    overdue = _saved("overdue", every_hours=1, last_run_at=_ago(5))
    _saved("recent", every_hours=6, last_run_at=_ago(1))
    _saved("manual only", every_hours=None, last_run_at=_ago(100))
    _saved("disabled", every_hours=1, enabled=0, last_run_at=_ago(100))
    due = scheduler.due_searches()
    assert [s["id"] for s in due] == [overdue, never]  # most overdue first (never-run is due "now")
    s = db.get_saved_search(overdue)
    assert scheduler.next_run_at(s) == (datetime.fromisoformat(s["last_run_at"]) + timedelta(hours=1)).isoformat()
    assert scheduler.next_run_at({**s, "every_hours": None}) is None


def test_scheduled_run_records_history_and_raises_alerts(fake_site):
    sid = _saved("Data jobs", mandatory="python", every_hours=6, notify=0)
    runner = search.SearchRunner()
    assert asyncio.run(scheduler.Scheduler(runner).tick(jitter=0)) is True
    st = runner.state
    assert st["trigger"] == "schedule" and st["search"] == "Data jobs" and st["new_jobs"] == 1 and st["alerts"] == 1
    assert any('Saved search "Data jobs"' in line for line in st["log"])
    run = db.search_runs()[0]
    assert (run["search_id"], run["trigger"], run["new_jobs"], run["errors"]) == (sid, "schedule", 1, [])
    assert "Done." in run["log_text"]
    assert db.get_saved_search(sid)["last_run_at"] == st["started_at"]
    a = db.alerts()
    assert len(a) == 1 and a[0]["job_id"] == "acme/external:R1" and a[0]["search_name"] == "Data jobs"
    assert a[0]["title"] == "Data Engineer"
    digest = alerts.digest_path()
    assert digest.exists() and "Data Engineer" in digest.read_text(encoding="utf-8")
    # not due again for 6 hours; a second tick does nothing
    assert asyncio.run(scheduler.Scheduler(search.SearchRunner()).tick(jitter=0)) is False


def test_alert_threshold_and_manual_runs(fake_site):
    db.save_resume("cv.txt", "Accountant. Excel, month-end close.", SAMPLE_MASTER, 0)  # a poor match for the posting
    sid = _saved("Strict", every_hours=1, notify=80)
    runner = search.SearchRunner()
    asyncio.run(scheduler.Scheduler(runner).tick(jitter=0))
    assert runner.state["new_jobs"] == 1 and runner.state["alerts"] == 0  # scored under 80
    db.update_saved_search(sid, {"notify_min_score": 0})
    manual = search.SearchRunner()

    async def go():
        manual.start(True, spec=db.get_saved_search(sid), trigger="manual")
        await manual.task

    asyncio.run(go())
    assert manual.state["alerts"] == 0  # you were watching: manual runs don't alert
    assert db.search_runs()[0]["trigger"] == "manual"


def test_saved_search_keeps_its_own_incremental_cursor(fake_site):
    db.save_settings({"mandatory": "sql"})  # the keywords in Settings differ from the saved search's
    _saved("Python", mandatory="python", every_hours=1)
    asyncio.run(scheduler.Scheduler(search.SearchRunner()).tick(jitter=0))
    assert all(b["searchText"] == "python" for b in fake_site.list_bodies)
    sig = search.keyword_signature(["python"], [])
    assert db.last_success("acme/external", sig) is not None
    assert db.last_success("acme/external", search.keyword_signature(["sql"], [])) is None


def test_run_due_once_runs_every_due_search(fake_site):
    _saved("A", mandatory="python", every_hours=1)
    _saved("B", mandatory="python", optional="aws", every_hours=1)
    _saved("C", mandatory="python", every_hours=None)
    results = asyncio.run(scheduler.run_due_once())
    assert [r["name"] for r in results] == ["A", "B"]
    assert asyncio.run(scheduler.run_due_once()) == []
    assert [r["trigger"] for r in db.search_runs()] == ["headless", "headless"]


# ---------------------------------------------------------------- alerts
def test_alerts_are_deduplicated_and_marked_seen():
    db.upsert_jobs([make_job("a:1"), make_job("a:2")])
    sid = _saved()
    assert db.add_alerts([("a:1", sid, 80), ("a:2", sid, 60)]) == 2
    assert db.add_alerts([("a:1", sid, 80)]) == 0  # already alerted for this search
    assert [a["job_id"] for a in db.alerts()] == ["a:1", "a:2"]  # best score first
    db.mark_alerts_seen([db.alerts()[0]["id"]])
    assert [a["job_id"] for a in db.alerts()] == ["a:2"]
    db.mark_alerts_seen()
    assert db.alerts() == [] and len(db.alerts(unseen_only=False)) == 2
    db.delete_saved_search(sid)
    assert db.alerts(unseen_only=False) == []


def test_purge_drops_alerts_of_purged_jobs_and_trims_history(monkeypatch):
    old = (datetime.now() - timedelta(days=30)).date().isoformat()
    db.upsert_job(make_job("a:old", posted_date=old))
    db.add_alerts([("a:old", None, 70)])
    monkeypatch.setattr(db, "KEEP_RUNS", 3)
    for i in range(5):
        db.record_run({"trigger": "manual", "started": f"2026-01-0{i + 1}T00:00:00"})
    db.purge_old()
    assert db.alerts(unseen_only=False) == []
    assert [r["started"][:10] for r in db.search_runs()] == ["2026-01-05", "2026-01-04", "2026-01-03"]


def test_digest_is_none_without_alerts():
    assert alerts.write_digest() is None


# ---------------------------------------------------------------- API
@pytest.fixture
def client():
    with TestClient(main.app) as c:
        c.get(f"/?key={main.SESSION_KEY}", follow_redirects=False)
        yield c


def test_saved_search_api(client):
    r = client.post("/api/searches", json={"name": "Data", "mandatory": "python", "every_hours": 6, "notify_min_score": 70})
    s = r.json()
    assert r.status_code == 200 and s["enabled"] is True and s["next_run_at"]  # never run: due now
    assert client.post("/api/searches", json={"name": "Empty"}).status_code == 422
    assert client.post("/api/searches", json={"name": "x", "mandatory": "a", "every_hours": 0}).status_code == 422
    s = client.put(f"/api/searches/{s['id']}", json={"every_hours": None, "name": None, "enabled": False}).json()
    assert s["every_hours"] is None and s["name"] == "Data" and s["enabled"] is False and s["next_run_at"] is None
    assert client.put(f"/api/searches/{s['id']}", json={"mandatory": "", "optional": ""}).status_code == 400
    assert [x["name"] for x in client.get("/api/searches").json()] == ["Data"]
    assert client.post("/api/search/run", json={"search_id": 999}).status_code == 404
    assert client.delete(f"/api/searches/{s['id']}").json() == {"ok": True}
    assert client.delete(f"/api/searches/{s['id']}").status_code == 404


def test_alerts_visit_runs_and_schedule_help(client):
    db.upsert_job(make_job("a:1"))
    db.add_alerts([("a:1", None, 75)])
    body = client.get("/api/alerts").json()
    assert [a["job_id"] for a in body["alerts"]] == ["a:1"] and body["digest"] is None
    client.post("/api/alerts/seen", json={"ids": None})
    assert client.get("/api/alerts").json()["alerts"] == []
    first = client.post("/api/visit").json()["previous"]
    second = client.post("/api/visit").json()["previous"]
    assert first is None and second
    db.record_run({"trigger": "manual", "started": "2026-01-01T00:00:00"})
    assert client.get("/api/search/runs?limit=5").json()[0]["trigger"] == "manual"
    h = client.get("/api/schedule/help").json()
    assert "--run-searches" in h["command"] and str(config.HOME) in h["command"] and h["register"]
    assert "first_seen" in client.get("/api/jobs").json()[0]


# ---------------------------------------------------------------- run.py without a server
def _run_py(*args, home):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("JOB_AGENT_HOME", None)
    return subprocess.run([sys.executable, str(ROOT / "run.py"), "--home", str(home), "--port", "8990", *args],
                          capture_output=True, text=True, timeout=120, env=env, cwd=str(ROOT))


def test_run_py_headless_modes(tmp_path):
    home = tmp_path / "home"
    out = _run_py("--schedule-help", home=home)
    assert out.returncode == 0 and "--run-searches" in out.stdout and str(home) in out.stdout
    out = _run_py("--run-searches", home=home)
    assert out.returncode == 0, out.stderr
    assert "No saved search is due." in out.stdout
    assert (home / "jobs.db").exists()
