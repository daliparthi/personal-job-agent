from datetime import date, timedelta

from app import db
from app.config import RETENTION_DAYS
from tests.conftest import make_job

OLD = (date.today() - timedelta(days=365)).isoformat()
RECENT = date.today().isoformat()


def test_purge_keeps_jobs_you_worked_on_forever():
    db.upsert_job(make_job("a:new-old", posted_date=OLD))
    db.upsert_job(make_job("a:new-old-hidden", posted_date=OLD))
    db.update_job("a:new-old-hidden", hidden=1)
    db.upsert_job(make_job("a:new-undated", posted_date=None))
    db.upsert_job(make_job("a:new-recent", posted_date=RECENT))
    for status in ("tailored", "saved", "applying", "applied"):
        db.upsert_job(make_job(f"a:{status}", posted_date=OLD))
        db.update_job(f"a:{status}", status=status)
    db.upsert_job(make_job("a:folder", posted_date=OLD))
    db.update_job("a:folder", folder="C:/somewhere")
    db.upsert_job(make_job("a:tailored-row", posted_date=OLD))
    db.save_tailored("a:tailored-row", {"resume": {}}, 70, [], [])
    db.save_tailored("a:gone", {"resume": {}}, 70, [], [])  # orphan: its job is not in the table

    assert db.purge_old() == 3
    left = {j["id"] for j in db.all_jobs()}
    assert left == {"a:new-recent", "a:tailored", "a:saved", "a:applying", "a:applied", "a:folder", "a:tailored-row"}
    assert db.get_tailored("a:tailored-row") is not None
    assert db.get_tailored("a:gone") is None


def test_purge_cutoff_is_the_retention_window():
    edge = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    past = (date.today() - timedelta(days=RETENTION_DAYS + 1)).isoformat()
    db.upsert_job(make_job("a:edge", posted_date=edge))
    db.upsert_job(make_job("a:past", posted_date=past))
    assert db.purge_old() == 1
    assert db.get_job("a:edge") and not db.get_job("a:past")


def test_upsert_refresh_keeps_your_fields():
    db.upsert_job(make_job())
    db.update_job("acme/external:R1", hidden=1, status="applied", folder="F")
    first_seen = db.get_job("acme/external:R1")["first_seen"]
    db.upsert_job(make_job(title="Data Engineer II", salary_max=160000.0))
    j = db.get_job("acme/external:R1")
    assert (j["hidden"], j["status"], j["folder"], j["first_seen"]) == (1, "applied", "F", first_seen)
    assert j["title"] == "Data Engineer II" and j["salary_max"] == 160000.0
    assert j["locations"] == ["Austin, TX"] and j["states"] == ["TX"]


def test_update_job_encodes_json_fields():
    db.upsert_job(make_job())
    db.update_job("acme/external:R1", matched=["Python"], missing=[{"keyword": "Spark"}], match_score=55)
    j = db.get_job("acme/external:R1")
    assert j["matched"] == ["Python"] and j["missing"] == [{"keyword": "Spark"}] and j["match_score"] == 55


def test_settings_merge_with_defaults():
    s = db.get_settings()
    assert s["filters"]["include_no_salary"] is True and s["max_bullets"] == 12
    db.save_settings({"filters": {"remote_only": True}, "mandatory": "python", "unknown_key": 1})
    s = db.get_settings()
    assert s["filters"]["remote_only"] is True
    assert s["filters"]["include_no_salary"] is True  # nested defaults survive a partial save
    assert s["mandatory"] == "python" and "unknown_key" not in s
    assert db.DEFAULT_SETTINGS["filters"]["remote_only"] is False  # defaults are never mutated


def test_tailored_round_trip():
    doc = {"resume": {"name": "J"}, "changes": {"sections.0.text": {"orig": "a", "alt": "b", "state": "alt"}}}
    db.save_tailored("j", doc, 61, ["Spark"], ["Go"])
    t = db.get_tailored("j")
    assert t["doc"] == doc and t["score_after"] == 61 and t["approved"] == ["Spark"] and t["rejected"] == ["Go"]


def test_company_runs():
    from datetime import datetime
    assert db.last_success("acme/x", "sig") is None
    when = datetime(2026, 10, 1, 9, 30)
    db.mark_success("acme/x", "sig", when)
    assert db.last_success("acme/x", "sig") == when
    assert db.last_run_overall() == "2026-10-01T09:30:00"
