"""The storage layer: schema migrations, concurrent access, batched writes and the precomputed keyword checks."""
import asyncio
import sqlite3
import threading
import time

import httpx
import pytest

from app import config, db, search
from app.workday import WorkdayClient
from tests.conftest import FIXTURES, make_job


def _legacy_db():
    """Turn jobs.db into one made by Job Agent before migrations existed (user_version 0, the original tables)."""
    db.wipe()
    c = sqlite3.connect(config.DB_PATH)
    c.executescript((FIXTURES / "legacy_jobs_db.sql").read_text(encoding="utf-8"))
    c.commit()
    c.close()


def test_legacy_database_upgrades_without_losing_anything():
    _legacy_db()
    db.init()
    assert db.schema_version() == db.SCHEMA_VERSION
    applied = db.get_job("acme/external:OLD1")
    assert applied["status"] == "applied" and applied["folder"].endswith("Data Engineer - OLD1")
    assert applied["matched"] == ["Python", "SQL"] and applied["optional_hits"] == []
    assert db.get_tailored("acme/external:OLD1")["approved"] == ["Spark"]
    assert db.get_settings()["mandatory"] == "sql" and db.get_settings()["filters"]["include_no_salary"] is True
    assert db.get_resume()["filename"] == "cv.docx"
    assert db.count_jobs() == 2
    indexes = {r[0] for r in db.read().execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"jobs_company", "jobs_status", "jobs_list"} <= indexes
    # the year-old applied job survives the purge; so does everything else here
    assert db.purge_old() == 0
    db.init()  # running again is a no-op
    assert db.schema_version() == db.SCHEMA_VERSION and db.count_jobs() == 2


def test_failed_migration_rolls_back(monkeypatch):
    _legacy_db()
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS[:2], "ALTER TABLE jobs ADD COLUMN kw_sig TEXT; THIS IS NOT SQL;"])
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    with pytest.raises(sqlite3.OperationalError):
        db.init()
    assert db.schema_version() == 2
    cols = {r[1] for r in db.read().execute("PRAGMA table_info(jobs)")}
    assert "kw_sig" not in cols  # the half-applied step was rolled back


def test_wal_mode_and_connection_reuse():
    assert db.read().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.read() is db.read()
    other = []
    t = threading.Thread(target=lambda: other.append(db.read()))
    t.start()
    t.join()
    assert other[0] is not db.read()  # one connection per thread


def test_concurrent_writers_do_not_fail():
    errors = []

    def writer(n):
        try:
            for i in range(30):
                db.upsert_job(make_job(f"t{n}:{i}"))
                db.update_job(f"t{n}:{i}", match_score=i)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and db.count_jobs() == 120


def test_write_transaction_rolls_back_on_error():
    with pytest.raises(RuntimeError):
        with db.conn() as c:
            c.execute("INSERT INTO settings(key, value) VALUES('mandatory', '\"x\"')")
            raise RuntimeError("boom")
    assert db.get_settings()["mandatory"] == ""


def test_batch_helpers():
    db.upsert_jobs([make_job("a:1", company_key="a"), make_job("a:2", company_key="a"), make_job("b:1", company_key="b")])
    assert db.job_ids("a") == {"a:1", "a:2"}
    db.touch_jobs(["a:1", "a:2"])
    db.set_scores([("a:1", 77, ["Python"], [{"keyword": "Spark"}], [{"kind": "degree", "label": "x"}]),
                   ("a:2", None, [], [], [])])
    assert db.get_job("a:1")["knockouts"] == [{"kind": "degree", "label": "x"}]
    assert db.get_job("a:1")["match_score"] == 77 and db.get_job("a:1")["missing"] == [{"keyword": "Spark"}]
    assert db.get_job("a:2")["match_score"] is None


def test_keyword_checks_are_stored_and_reused():
    db.upsert_jobs([make_job("a:1", description_text="Python and SQL"), make_job("a:2", description_text="Excel")])
    assert search.refresh_keyword_hits(["python"], ["sql", "spark"]) == 2
    assert search.refresh_keyword_hits(["python"], ["sql", "spark"]) == 0  # same keywords: nothing to redo
    sig = search.keyword_hits_signature(["python"], ["sql", "spark"])
    rows = {j["id"]: j for j in db.jobs_for_list(["Full-time"], kw_sig=sig)}
    assert list(rows) == ["a:1"] and rows["a:1"]["optional_hits"] == ["sql"]
    assert search.refresh_keyword_hits([], ["excel"]) == 2  # new keywords: every job re-checked once


def test_list_jobs_is_fast_with_thousands_of_postings():
    jobs = [make_job(f"bulk:{i}", match_score=i % 100, description_text=("Python SQL Spark AWS " * 200),
                     employment_type="Full-time" if i % 3 else "Contract") for i in range(5000)]
    db.upsert_jobs(jobs)
    s = db.get_settings()
    s["mandatory"], s["optional"] = "python", "spark, kafka"
    search.list_jobs(s)  # first call checks the keywords for every job once
    started = time.perf_counter()
    out = search.list_jobs(s)
    elapsed = time.perf_counter() - started
    assert len(out) == 5000 and out[0]["match_score"] == 99 and out[0]["optional_hits"] == ["spark"]
    assert elapsed < 1.0, f"listing 5,000 jobs took {elapsed:.3f}s"  # ~0.1 s on a laptop; generous for CI


def test_rescorer_runs_in_the_background_and_coalesces(monkeypatch):
    calls, gate = [], threading.Event()

    def slow():
        calls.append(1)
        gate.wait(5)
        return 0

    monkeypatch.setattr(search, "rescore_all", slow)
    r = search.Rescorer()
    r.request()
    assert r.running
    r.request()
    r.request()  # both arrive while the first pass runs: one more pass afterwards, not two
    gate.set()
    assert r.wait(5)
    assert len(calls) == 2 and not r.running


def test_rescorer_reports_errors(monkeypatch):
    def boom():
        raise ValueError("bad")
    monkeypatch.setattr(search, "rescore_all", boom)
    r = search.Rescorer()
    r.request()
    assert r.wait(5) and r.last_error == "ValueError('bad')"


def test_company_list_cache_follows_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "companies.yaml"
    path.write_text('companies:\n  - name: "Acme"\n    url: https://acme.wd1.myworkdayjobs.com/X\n', encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    first = search.load_companies()
    first[0]["name"] = "changed by a caller"
    assert search.load_companies()[0]["name"] == "Acme"  # callers get copies
    path.write_text('companies:\n  - name: "Acme Corp"\n    url: https://acme.wd1.myworkdayjobs.com/X\n',
                    encoding="utf-8")
    assert search.load_companies()[0]["name"] == "Acme Corp"


def test_search_does_database_work_off_the_event_loop(tmp_path, monkeypatch):
    """SQLite and scoring must not run on the event loop thread (it also serves the page)."""
    from tests.test_search import ACME_URL, FakeWorkday
    path = tmp_path / "companies.yaml"
    path.write_text(f'companies:\n  - name: "Acme"\n    url: {ACME_URL}\n', encoding="utf-8")
    monkeypatch.setattr(search, "COMPANIES_SHARED", path)
    monkeypatch.setattr(search, "WorkdayClient", lambda: WorkdayClient(transport=httpx.MockTransport(FakeWorkday())))
    db.save_settings({"mandatory": "python"})
    db.save_resume("cv.txt", "Python SQL", {"name": "J", "sections": []}, 0)
    loop_thread, threads = [], []
    real_upsert, real_score = db.upsert_jobs, search.scoring.score

    def spy_upsert(jobs):
        threads.append(threading.current_thread())
        return real_upsert(jobs)

    def spy_score(*a, **k):
        threads.append(threading.current_thread())
        return real_score(*a, **k)

    monkeypatch.setattr(db, "upsert_jobs", spy_upsert)
    monkeypatch.setattr(search.scoring, "score", spy_score)
    runner = search.SearchRunner()

    async def go():
        loop_thread.append(threading.current_thread())
        runner.start(False)
        await runner.task

    asyncio.run(go())
    assert runner.state["new_jobs"] == 1
    assert threads and all(t is not loop_thread[0] for t in threads)
