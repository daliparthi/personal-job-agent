"""The application pipeline: where each job you worked on stands, its timeline, follow-ups and statistics.

Statuses, in order: new -> tailored -> saved -> applying -> applied -> screening -> interviewing -> offer, or one of
the outcomes rejected / withdrawn / ghosted. Job Agent moves a job forward by itself when you tailor, package, open
the apply window or submit, and never backwards (re-packaging a job you are interviewing for keeps "interviewing").
On the Pipeline board you can move a job anywhere.

Every change is recorded in job_events (the timeline) and, when the job has an application folder, mirrored into its
application.json, so the folder stays a complete record on its own. Jobs past "new" are never purged.
"""
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, sources
from .config import APPLICATIONS
from .jobparse import states_in

STATUSES = {  # status: (rank, label)
    "new": (0, "New"), "tailored": (1, "Tailored"), "saved": (2, "Saved"), "applying": (3, "Applying"),
    "applied": (4, "Applied"), "screening": (5, "Recruiter screen"), "interviewing": (6, "Interviewing"),
    "offer": (7, "Offer"), "rejected": (8, "Rejected"), "withdrawn": (8, "Withdrawn"), "ghosted": (8, "Ghosted"),
}
OUTCOMES = ("rejected", "withdrawn", "ghosted")
WAITING = ("applied", "screening", "interviewing")  # waiting on the company: these can go quiet
CHECK_STILL_POSTED = ("tailored", "saved", "applying", "applied", "screening", "interviewing")
RESPONDED = {"screening", "interviewing", "offer", "rejected"}
INTERVIEWED = {"interviewing", "offer"}
# Board columns: (key, label, statuses shown there, status a card dropped there gets)
BOARD = [
    ("preparing", "Preparing", ("tailored", "saved", "applying"), "saved"),
    ("applied", "Applied", ("applied",), "applied"),
    ("screening", "Recruiter screen", ("screening",), "screening"),
    ("interviewing", "Interviewing", ("interviewing",), "interviewing"),
    ("offer", "Offer", ("offer",), "offer"),
    ("rejected", "Rejected", ("rejected",), "rejected"),
    ("withdrawn", "Withdrawn", ("withdrawn",), "withdrawn"),
    ("ghosted", "Ghosted", ("ghosted",), "ghosted"),
]
SCORE_BUCKETS = [("under 40", 0, 40), ("40–59", 40, 60), ("60–79", 60, 80), ("80+", 80, 1000)]


def rank(status) -> int:
    return STATUSES.get(status or "new", (0, ""))[0]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _day(iso):
    return date.fromisoformat(iso[:10]) if iso else None


# ---------------------------------------------------------------- changes
def set_status(job_id, status, note=None, force=True) -> bool:
    """Move a job to `status`. Automatic moves (force=False) only go forward. Returns whether it changed."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    job = db.get_job(job_id)
    if not job:
        raise KeyError(job_id)
    current = job.get("status") or "new"
    if current == status or (not force and rank(current) >= rank(status)):
        return False
    at = _now()
    db.set_status(job_id, status, at)
    db.add_event(job_id, "status", note=note, from_status=current, to_status=status, at=at)
    write_record(job_id)
    return True


def advance(job_id, status, how) -> bool:
    """What Job Agent does on its own (tailored, packaged, apply window opened, submitted): forward only."""
    return set_status(job_id, status, note=how, force=False)


def add_note(job_id, text):
    text = (text or "").strip()
    if not text:
        raise ValueError("The note is empty")
    db.add_event(job_id, "note", note=text[:4000])
    write_record(job_id)


def set_follow_up(job_id, on, action=""):
    """on: a date (or None to clear); action: what to do then, e.g. "Email the recruiter"."""
    action = (action or "").strip()[:200]
    day = on.isoformat() if isinstance(on, date) else (on or None)
    if day:
        date.fromisoformat(day)  # validates
    db.set_follow_up(job_id, day, action or None)
    db.add_event(job_id, "follow_up", note=f"{action or 'Follow up'} on {day}" if day else "Follow-up cleared")
    write_record(job_id)


def record_check(job_id, closed: bool):
    """A search checked whether a tracked posting is still on the company's site."""
    at = _now()
    db.set_checked(job_id, at, closed)
    if closed:
        db.add_event(job_id, "closed", note="The posting is no longer on the company's career site", at=at)
        write_record(job_id)


def write_record(job_id):
    """Mirror the status and timeline into the job's application.json (when it has an application folder)."""
    job = db.get_job(job_id)
    if not job or not job.get("folder"):
        return
    meta_path = Path(job["folder"]) / "application.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    events = db.job_events(job_id)
    meta["status"] = job["status"]
    meta["status_at"] = job.get("status_at")
    if rank(job["status"]) >= rank("applied") and not meta.get("applied_at"):
        meta["applied_at"] = next((e["at"] for e in events if e["to_status"] == "applied"), job.get("status_at"))
    meta["next_action_at"], meta["next_action"] = job.get("next_action_at"), job.get("next_action")
    if job.get("closed_at"):
        meta["posting_closed_at"] = job["closed_at"]
    meta["history"] = events
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- views
def board(ghost_after_days=21) -> dict:
    today = date.today()
    columns = {key: [] for key, *_ in BOARD}
    column_of = {s: key for key, _, statuses, _ in BOARD for s in statuses}
    due = []
    for j in db.tracked_jobs():
        col = column_of.get(j["status"])
        if not col:
            continue
        since = _day(j.get("status_at") or j.get("first_seen"))
        j["days_in_status"] = (today - since).days if since else None
        j["suggest_ghosted"] = (j["status"] in WAITING and j["days_in_status"] is not None
                                and j["days_in_status"] >= ghost_after_days)
        j["follow_up_due"] = bool(j.get("next_action_at") and j["next_action_at"] <= today.isoformat())
        columns[col].append(j)
        if j["follow_up_due"]:
            due.append(j)
    for jobs in columns.values():
        jobs.sort(key=lambda j: j.get("status_at") or "", reverse=True)
    return {"columns": [{"key": k, "label": label, "drop_status": drop, "jobs": columns[k]}
                        for k, label, _, drop in BOARD],
            "labels": {s: label for s, (_, label) in STATUSES.items()},
            "due": sorted(due, key=lambda j: j["next_action_at"]), "ghost_after_days": ghost_after_days}


def stats(weeks=12) -> dict:
    """Applications per week, response and interview rates, and interview rate by match score."""
    jobs = {j["id"]: j for j in db.tracked_jobs()}
    reached, applied_at = defaultdict(set), {}
    for job_id, to_status, at in db.status_events():
        reached[job_id].add(to_status)
        if to_status == "applied":
            applied_at.setdefault(job_id, at)
    for job_id, j in jobs.items():
        reached[job_id].add(j["status"])
        # Moved straight past "applied" on the board (e.g. to Interviewing, or Rejected): it was applied for.
        # Withdrawn alone doesn't say so: you may have withdrawn before applying.
        past_applied = rank(j["status"]) >= rank("applied") and j["status"] != "withdrawn"
        if past_applied or reached[job_id] & RESPONDED:
            applied_at.setdefault(job_id, j.get("status_at") or j.get("first_seen"))
    applied = [job_id for job_id in applied_at if job_id in jobs]
    responded = [i for i in applied if reached[i] & RESPONDED]
    interviewed = [i for i in applied if reached[i] & INTERVIEWED]
    offers = [i for i in applied if "offer" in reached[i]]

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week_starts = [monday - timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]
    per_week = Counter()
    for i in applied:
        d = _day(applied_at[i])
        if d:
            per_week[d - timedelta(days=d.weekday())] += 1

    by_score = []
    for label, lo, hi in SCORE_BUCKETS:
        ids = [i for i in applied if lo <= _score(jobs[i]) < hi]
        hits = sum(1 for i in ids if i in interviewed)
        by_score.append({"bucket": label, "applied": len(ids), "interviews": hits,
                         "rate": round(100 * hits / len(ids)) if ids else None})
    unscored = sum(1 for i in applied if _score(jobs[i]) < 0)

    def pct(part):
        return round(100 * len(part) / len(applied)) if applied else None

    return {"applied": len(applied), "responses": len(responded), "interviews": len(interviewed),
            "offers": len(offers), "response_rate": pct(responded), "interview_rate": pct(interviewed),
            "waiting": sum(1 for j in jobs.values() if j["status"] in WAITING),
            "per_week": [{"week": w.isoformat(), "applied": per_week.get(w, 0)} for w in week_starts],
            "by_score": by_score, "unscored": unscored}


def _score(job) -> int:
    s = job.get("score_after") if job.get("score_after") is not None else job.get("match_score")
    return -1 if s is None else s


# ---------------------------------------------------------------- recovery
def backfill_from_disk() -> int:
    """Rebuild database rows from Applications/*/*/application.json for jobs the database no longer has (earlier
    versions purged applied jobs after 7 days). Returns how many jobs were restored."""
    restored = 0
    for meta_path in sorted(APPLICATIONS.glob("*/*/application.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        job_id = meta.get("job_id")
        if not isinstance(job_id, str) or ":" not in job_id:
            continue
        folder = str(meta_path.parent)
        status = meta.get("status") if meta.get("status") in STATUSES else (
            "applied" if meta.get("applied_at") else "saved")  # "prepared" packages are "saved"
        existing = db.get_job(job_id)
        if existing:  # the database is the source of truth; only fill in what it is missing
            if not existing.get("folder"):
                db.update_job(job_id, folder=folder)
            if rank(status) > rank(existing.get("status")):
                set_status(job_id, status, note="restored from application.json")
            continue
        db.upsert_job(_job_from_meta(job_id, meta, meta_path.parent, status))
        at = meta.get("applied_at") or meta.get("prepared_at") or _now()
        db.add_event(job_id, "status", to_status=status, note="restored from the Applications folder", at=at)
        restored += 1
    return restored


def _job_from_meta(job_id, meta, folder: Path, status) -> dict:
    key = job_id.split(":", 1)[0]
    tenant, _, site = key.partition("/")
    locations = meta.get("location") or []
    if isinstance(locations, str):
        locations = [locations]
    text = ""
    jd = folder / "job_description.txt"
    if jd.exists():  # title, company, url, blank line, then the description
        text = "\n".join(jd.read_text(encoding="utf-8").split("\n")[4:]).strip()
    at = meta.get("applied_at") or meta.get("prepared_at") or _now()
    return {
        "id": job_id, "company": meta.get("company") or tenant, "company_key": key, "tenant": tenant, "site": site,
        "source": sources.source_of_key(key), "title": meta.get("title") or "", "url": meta.get("url"), "req_id": meta.get("req_id"),
        "external_path": None, "location": locations[0] if locations else "", "locations_json": locations,
        "states_json": sorted(set().union(*(states_in(loc) for loc in locations))) if locations else [],
        "country": "United States", "remote_type": meta.get("work_mode") or "Unspecified", "remote_raw": "",
        "employment_type": meta.get("employment_type") or "Full-time", "worker_sub_type": "", "time_type": "",
        "salary_min": meta.get("salary_min"), "salary_max": meta.get("salary_max"), "salary_text": "",
        "posted_date": meta.get("posted_date"), "description_html": "", "description_text": text,
        "match_score": meta.get("match_score_before"), "status": status, "status_at": at, "folder": str(folder),
        "first_seen": meta.get("prepared_at") or at,
    }
