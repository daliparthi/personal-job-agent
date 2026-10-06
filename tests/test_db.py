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


# ---------------------------------------------------------------- database size
def _bulky(job_id, **over):
    text = "Python SQL AWS " * 4000  # about 60 KB of description, as text and as HTML
    return make_job(job_id, description_text=text, description_html=f"<p>{text}</p>", **over)


def test_retention_default_is_five_days():
    assert RETENTION_DAYS == 5
    assert db.DEFAULT_SETTINGS["keep_new_days"] == 5


def test_size_limit_drops_oldest_untouched_first_and_keeps_applied():
    for n in range(40):
        db.upsert_job(_bulky(f"a:new{n}", posted_date=(date.today() - timedelta(days=n % 5)).isoformat()))
    db.upsert_job(_bulky("a:applied", posted_date=OLD))
    db.update_job("a:applied", status="applied")
    db._shrink()  # fold the write-ahead log in, so the size below is the database itself
    start = db.db_bytes()
    assert start > 2_000_000

    removed = db.enforce_size_limit(max_bytes=start // 2)

    assert removed > 0 and db.db_bytes() <= start // 2
    assert db.get_job("a:applied")  # worked-on jobs are never size-purged
    left = {j["id"] for j in db.all_jobs()}
    oldest_gone = [f"a:new{n}" for n in range(40) if n % 5 == 4 and f"a:new{n}" not in left]
    newest_kept = [f"a:new{n}" for n in range(40) if n % 5 == 0 and f"a:new{n}" in left]
    assert oldest_gone and newest_kept  # the 4-day-old postings went before today's


def test_size_limit_trims_html_of_worked_on_jobs_as_a_last_resort():
    for n in range(10):
        db.upsert_job(_bulky(f"a:app{n}", posted_date=OLD))
        db.update_job(f"a:app{n}", status="applied")
    db._shrink()
    start = db.db_bytes()
    assert db.enforce_size_limit(max_bytes=start // 2) == 0
    jobs = [db.get_job(f"a:app{n}") for n in range(10)]
    assert all(j for j in jobs)  # nothing deleted
    assert any(j["description_html"] == "" for j in jobs) and all(j["description_text"] for j in jobs)
    assert db.db_bytes() < start


def test_purge_old_frees_file_space():
    for n in range(40):
        db.upsert_job(_bulky(f"a:old{n}", posted_date=OLD))
    big = db.db_bytes()
    assert db.purge_old() == 40
    assert db.db_bytes() < big // 4


def test_under_the_limit_nothing_extra_is_removed():
    db.upsert_job(_bulky("a:today", posted_date=RECENT))
    assert db.enforce_size_limit() == 0 and db.get_job("a:today")
