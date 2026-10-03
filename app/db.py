"""SQLite storage. Untouched postings older than RETENTION_DAYS are purged automatically; jobs you worked on
(tailored, saved, applying, applied) are kept forever."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from .config import DB_PATH, RETENTION_DAYS

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    company TEXT, company_key TEXT, tenant TEXT, site TEXT,
    title TEXT, url TEXT, external_path TEXT, req_id TEXT,
    location TEXT, locations_json TEXT, states_json TEXT, country TEXT,
    remote_type TEXT, remote_raw TEXT,
    employment_type TEXT, worker_sub_type TEXT, time_type TEXT,
    salary_min REAL, salary_max REAL, salary_text TEXT,
    posted_date TEXT,
    description_html TEXT, description_text TEXT,
    match_score INTEGER, matched_json TEXT, missing_json TEXT,
    hidden INTEGER DEFAULT 0,
    status TEXT DEFAULT 'new',
    folder TEXT,
    first_seen TEXT, last_seen TEXT
);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(posted_date);
CREATE TABLE IF NOT EXISTS company_runs (
    company_key TEXT, sig TEXT, last_success TEXT,
    PRIMARY KEY (company_key, sig)
);
CREATE TABLE IF NOT EXISTS tailored (
    job_id TEXT PRIMARY KEY, doc TEXT, score_after INTEGER,
    approved_json TEXT, rejected_json TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- copy of master_resume.yaml (the file is the source of truth; text is what the match score reads)
CREATE TABLE IF NOT EXISTS resume (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    filename TEXT, text TEXT, data TEXT, yaml_mtime REAL, uploaded_at TEXT
);
"""


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def purge_old() -> int:
    """Drop untouched postings older than the retention window. Returns rows removed from jobs.

    Only status 'new' jobs with no application folder and no tailored resume expire. Anything you tailored,
    saved, are applying to or applied to is kept forever."""
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    stale_run = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    with conn() as c:
        n = c.execute("DELETE FROM jobs WHERE (posted_date IS NULL OR posted_date < ?) "
                      "AND COALESCE(status, 'new') = 'new' AND folder IS NULL "
                      "AND id NOT IN (SELECT job_id FROM tailored)", (cutoff,)).rowcount
        c.execute("DELETE FROM tailored WHERE job_id NOT IN (SELECT id FROM jobs)")
        c.execute("DELETE FROM company_runs WHERE last_success < ?", (stale_run,))
    return n


# ---------- settings ----------
DEFAULT_SETTINGS = {
    "mandatory": "",
    "optional": "",
    "current_employer": "",
    "disabled_companies": [],
    "filters": {
        "states": [],
        "city": "",
        "remote_only": False,
        "types": ["Full-time", "Part-time", "Contract", "Temporary", "Internship"],
        "min_salary": 0,
        "include_no_salary": True,
        "require_optional": False,
        "show_hidden": False,
    },
    "profile": {
        "first_name": "", "last_name": "", "email": "", "phone": "", "phone_type": "Mobile",
        "address1": "", "address2": "", "city": "", "state": "", "postal_code": "", "country": "United States of America",
        "linkedin": "", "website": "", "github": "",
        "authorized_us": "Yes", "needs_sponsorship": "No", "previously_employed": "No",
        "how_heard": "Company Website",
    },
    "engine": "auto",           # auto = CPU model by default, GPU when this browser has a usable one; "onnx" = CPU only
    "upload_format": "docx",
    "max_bullets": 12,
}


def get_settings() -> dict:
    with conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in stored.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def save_settings(values: dict):
    with conn() as c:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))


# ---------- resume ----------
def get_resume():
    with conn() as c:
        r = c.execute("SELECT * FROM resume WHERE id = 1").fetchone()
    if not r:
        return None
    return {"filename": r["filename"], "text": r["text"], "data": json.loads(r["data"]),
            "yaml_mtime": r["yaml_mtime"], "uploaded_at": r["uploaded_at"]}


def save_resume(filename, text, data, yaml_mtime, clear_tailored=False):
    with conn() as c:
        c.execute("INSERT INTO resume(id, filename, text, data, yaml_mtime, uploaded_at) VALUES(1, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(id) DO UPDATE SET filename=excluded.filename, text=excluded.text, "
                  "data=excluded.data, yaml_mtime=excluded.yaml_mtime, uploaded_at=excluded.uploaded_at",
                  (filename, text, json.dumps(data), yaml_mtime, datetime.now().isoformat(timespec="seconds")))
        if clear_tailored:  # a different resume: tailored copies of the old one no longer apply
            c.execute("DELETE FROM tailored")


def clear_resume():
    with conn() as c:
        c.execute("DELETE FROM resume")


# ---------- incremental-run bookkeeping ----------
def last_success(company_key, sig):
    with conn() as c:
        r = c.execute("SELECT last_success FROM company_runs WHERE company_key = ? AND sig = ?",
                      (company_key, sig)).fetchone()
    return datetime.fromisoformat(r["last_success"]) if r else None


def mark_success(company_key, sig, when: datetime):
    with conn() as c:
        c.execute("INSERT INTO company_runs(company_key, sig, last_success) VALUES(?, ?, ?) "
                  "ON CONFLICT(company_key, sig) DO UPDATE SET last_success = excluded.last_success",
                  (company_key, sig, when.isoformat(timespec="seconds")))


def last_run_overall():
    with conn() as c:
        r = c.execute("SELECT MAX(last_success) AS m FROM company_runs").fetchone()
    return r["m"] if r else None


# ---------- jobs ----------
JSON_COLS = ("locations_json", "states_json", "matched_json", "missing_json")


def job_exists(job_id) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None


def touch_job(job_id):
    with conn() as c:
        c.execute("UPDATE jobs SET last_seen = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), job_id))


def upsert_job(job: dict):
    now = datetime.now().isoformat(timespec="seconds")
    row = dict(job)
    for k in JSON_COLS:
        if k in row and not isinstance(row[k], str):
            row[k] = json.dumps(row[k])
    row.setdefault("first_seen", now)
    row["last_seen"] = now
    cols = list(row.keys())
    # Keep user-owned fields (hidden/status/folder/first_seen) when a job is refreshed.
    keep = {"hidden", "status", "folder", "first_seen"}
    updates = ", ".join(f"{k} = excluded.{k}" for k in cols if k not in keep and k != "id")
    with conn() as c:
        c.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)}) "
                  f"ON CONFLICT(id) DO UPDATE SET {updates}", [row[k] for k in cols])


def _decode(r) -> dict:
    d = dict(r)
    for k in JSON_COLS:
        if k in d:
            d[k.replace("_json", "")] = json.loads(d.pop(k) or "[]")
    return d


def all_jobs(with_text=False):
    cols = "*" if with_text else ", ".join([
        "id", "company", "company_key", "title", "url", "location", "locations_json", "states_json",
        "remote_type", "employment_type", "worker_sub_type", "time_type", "salary_min", "salary_max",
        "salary_text", "posted_date", "match_score", "hidden", "status", "folder", "description_text"])
    with conn() as c:
        rows = c.execute(f"SELECT {cols} FROM jobs").fetchall()
    return [_decode(r) for r in rows]


def get_job(job_id):
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _decode(r) if r else None


def update_job(job_id, **fields):
    for k in JSON_COLS:
        short = k.replace("_json", "")
        if short in fields:
            fields[k] = json.dumps(fields.pop(short))
    sets = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])


def get_tailored(job_id):
    with conn() as c:
        r = c.execute("SELECT * FROM tailored WHERE job_id = ?", (job_id,)).fetchone()
    if not r:
        return None
    return {"doc": json.loads(r["doc"]), "score_after": r["score_after"],
            "approved": json.loads(r["approved_json"] or "[]"), "rejected": json.loads(r["rejected_json"] or "[]"),
            "updated_at": r["updated_at"]}


def save_tailored(job_id, doc, score_after, approved, rejected):
    """doc = {"resume": tailored copy of the master YAML data, "changes": {path: {orig, alt, state, ...}}}"""
    with conn() as c:
        c.execute("INSERT INTO tailored(job_id, doc, score_after, approved_json, rejected_json, updated_at) "
                  "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET doc=excluded.doc, "
                  "score_after=excluded.score_after, approved_json=excluded.approved_json, "
                  "rejected_json=excluded.rejected_json, updated_at=excluded.updated_at",
                  (job_id, json.dumps(doc), score_after, json.dumps(approved), json.dumps(rejected),
                   datetime.now().isoformat(timespec="seconds")))
