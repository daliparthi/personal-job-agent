"""SQLite storage. Untouched postings older than RETENTION_DAYS are purged automatically; jobs you worked on
(tailored, saved, applying, applied) are kept forever. The file is also held under MAX_DB_BYTES (purge_old).

Connections: each thread keeps one connection to jobs.db (WAL mode, so readers never wait for a writer). Writes run
inside `conn()`, an IMMEDIATE transaction, so two writers queue on SQLite's own lock instead of failing half-way.

Schema: versioned with PRAGMA user_version. MIGRATIONS[n] upgrades a database from version n to n + 1; a database
made before migrations existed (version 0) upgrades in place without losing anything.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from .config import DB_PATH, MAX_DB_BYTES, RETENTION_DAYS

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

MIGRATIONS = [
    # 1: the original tables (IF NOT EXISTS: a database from before migrations keeps its data)
    SCHEMA,
    # 2: indexes for the job list and the per-company search bookkeeping
    """
    CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_key);
    CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS jobs_list ON jobs(hidden, employment_type);
    """,
    # 3: the keyword checks stored with each job, so listing jobs never re-reads every description
    """
    ALTER TABLE jobs ADD COLUMN kw_sig TEXT;
    ALTER TABLE jobs ADD COLUMN mandatory_ok INTEGER;
    ALTER TABLE jobs ADD COLUMN optional_hits_json TEXT;
    CREATE INDEX IF NOT EXISTS jobs_kw ON jobs(kw_sig);
    """,
    # 4: the application pipeline: when the status last changed, follow-ups, closed postings and a timeline
    """
    ALTER TABLE jobs ADD COLUMN status_at TEXT;
    ALTER TABLE jobs ADD COLUMN next_action_at TEXT;
    ALTER TABLE jobs ADD COLUMN next_action TEXT;
    ALTER TABLE jobs ADD COLUMN closed_at TEXT;
    ALTER TABLE jobs ADD COLUMN checked_at TEXT;
    CREATE TABLE IF NOT EXISTS job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
        from_status TEXT, to_status TEXT, note TEXT
    );
    CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, at);
    UPDATE jobs SET status_at = COALESCE(last_seen, first_seen) WHERE COALESCE(status, 'new') <> 'new';
    INSERT INTO job_events(job_id, at, kind, to_status, note)
        SELECT id, COALESCE(last_seen, first_seen, strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')), 'status',
               status, 'status before the pipeline tracker existed'
        FROM jobs WHERE COALESCE(status, 'new') <> 'new';
    """,
    # 5: saved searches (named keyword sets, optionally on a schedule), the run history, and new-match alerts
    """
    CREATE TABLE IF NOT EXISTS saved_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        mandatory TEXT NOT NULL DEFAULT '', optional TEXT NOT NULL DEFAULT '',
        every_hours INTEGER,          -- NULL: only when you click Run
        notify_min_score INTEGER,     -- NULL: no alerts
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS search_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_id INTEGER, search_name TEXT, mode TEXT, trigger TEXT,
        started TEXT, finished TEXT, new_jobs INTEGER, refreshed INTEGER, rejected INTEGER,
        errors_json TEXT, log_text TEXT
    );
    CREATE INDEX IF NOT EXISTS search_runs_started ON search_runs(started);
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL, search_id INTEGER, score INTEGER,
        created_at TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
        UNIQUE (job_id, search_id)
    );
    CREATE INDEX IF NOT EXISTS alerts_unseen ON alerts(seen, created_at);
    CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen);
    """,
    # 6: requirement-aware scoring: hard requirements you don't meet, and your thumbs up / down on a match
    """
    ALTER TABLE jobs ADD COLUMN knockouts_json TEXT;
    ALTER TABLE jobs ADD COLUMN feedback INTEGER;
    """,
    # 7: job boards other than Workday (Greenhouse, Lever, Ashby, SmartRecruiters)
    """
    ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'workday';
    """,
    # 8: the answer bank: your answers to application questions, reused (for review) on the next form that asks
    """
    CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY,
        question TEXT NOT NULL,
        norm TEXT NOT NULL UNIQUE,
        answer TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'text',
        source TEXT NOT NULL DEFAULT 'manual',
        company TEXT,
        uses INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_used_at TEXT
    );
    """,
    # 9: what a posting says about visa sponsorship / H-1B / H-4 EAD (comma-separated tags, e.g. "h1b,h4ead")
    """
    ALTER TABLE jobs ADD COLUMN sponsorship TEXT;
    """,
    # 10: the default retention went from 7 to 5 days: a saved "7" is the old default, not a choice
    """
    DELETE FROM settings WHERE key = 'keep_new_days' AND value = '7';
    """,
    # 11: omit words: a posting containing any of them is left out (saved searches keep their own list)
    """
    ALTER TABLE saved_searches ADD COLUMN omit TEXT NOT NULL DEFAULT '';
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)

# ---------------------------------------------------------------- connections
_local = threading.local()
_registry_lock = threading.Lock()
_open = {}         # connection -> the thread that owns it
_generation = 0    # bumped by close_all(); a thread whose connection is older replaces it on its next call


def _close(c):
    try:
        c.close()
    except sqlite3.Error:
        pass


def _connect():
    c = getattr(_local, "c", None)
    if c is not None and _local.gen != _generation:  # close_all() ran: drop our old connection (we own it)
        with _registry_lock:
            _open.pop(c, None)
        _close(c)
        c = None
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; much faster commits
        c.execute("PRAGMA journal_size_limit=4194304")  # the -wal file shrinks back after a checkpoint
        c.execute("PRAGMA foreign_keys=ON")
        with _registry_lock:
            _open[c] = threading.current_thread()
        _local.c, _local.gen = c, _generation
    return c


def close_all():
    """Close this thread's connection and those of threads that have ended (shutdown, tests).

    A connection still owned by a running thread is never closed from here, as that could pull it out from under a
    query in progress (a native crash); that thread closes it itself on its next database call."""
    global _generation
    mine = getattr(_local, "c", None)
    with _registry_lock:
        _generation += 1
        done = [c for c, owner in _open.items() if c is mine or not owner.is_alive()]
        for c in done:
            del _open[c]
    for c in done:
        _close(c)
    if mine is not None:
        _local.c = None


def wipe():
    """Drop every table (tests): the next init() builds the schema from scratch. Works while other threads still
    hold connections, unlike deleting the file (which Windows refuses)."""
    c = _connect()
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                      "AND name NOT LIKE 'sqlite_%'")]
    with conn() as w:
        for name in tables:
            w.execute(f'DROP TABLE IF EXISTS "{name}"')
        w.execute("PRAGMA user_version = 0")


@contextmanager
def conn():
    """A write transaction on this thread's connection. Nested use joins the outer transaction."""
    c = _connect()
    if c.in_transaction:
        yield c
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def read():
    """This thread's connection, for reads (WAL: never waits for a writer)."""
    return _connect()


def init():
    c = _connect()
    # Write-ahead logging is stored in the file (set once) and can't change inside a transaction.
    c.execute("PRAGMA journal_mode=WAL")
    version = c.execute("PRAGMA user_version").fetchone()[0]
    # A newer Job Agent may have added tables or columns; this version simply doesn't use them.
    for n in range(version, SCHEMA_VERSION):
        try:
            c.executescript(f"BEGIN IMMEDIATE;\n{MIGRATIONS[n]}\nPRAGMA user_version = {n + 1};\nCOMMIT;")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


def schema_version() -> int:
    return read().execute("PRAGMA user_version").fetchone()[0]


# A posting you never touched: still 'new', no application folder, tailored resume, timeline entry or follow-up.
# Only these ever expire; anything you tailored, saved, applied to or moved along the pipeline is kept forever.
UNTOUCHED = ("COALESCE(status, 'new') = 'new' AND folder IS NULL AND next_action_at IS NULL "
             "AND id NOT IN (SELECT job_id FROM tailored) AND id NOT IN (SELECT job_id FROM job_events)")
KEEP_RUNS = 200  # search runs kept in the history
SIZE_HEADROOM = 0.85  # when over the size limit, shrink to this share of it so the next search doesn't trip it again
ROW_OVERHEAD = 2048   # bytes a posting takes besides its description (other columns, indexes)


def _drop_orphans(c):
    c.execute("DELETE FROM tailored WHERE job_id NOT IN (SELECT id FROM jobs)")
    c.execute("DELETE FROM alerts WHERE job_id NOT IN (SELECT id FROM jobs)")


def purge_old(keep_days: int = RETENTION_DAYS) -> int:
    """Drop untouched postings older than keep_days (Settings: "Keep new postings"), then keep jobs.db under
    the size limit (see enforce_size_limit). Returns rows removed from jobs."""
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    stale_run = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    with conn() as c:
        n = c.execute(f"DELETE FROM jobs WHERE (posted_date IS NULL OR posted_date < ?) AND {UNTOUCHED}",
                      (cutoff,)).rowcount
        _drop_orphans(c)
        c.execute("DELETE FROM company_runs WHERE last_success < ?", (stale_run,))
        c.execute("DELETE FROM search_runs WHERE id NOT IN (SELECT id FROM search_runs ORDER BY id DESC LIMIT ?)",
                  (KEEP_RUNS,))
    return n + enforce_size_limit()


# ---------- size ----------
def db_bytes() -> int:
    """Size on disk of jobs.db together with its write-ahead log."""
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(f"{DB_PATH}{suffix}")
        except OSError:
            pass
    return total


def _free_bytes() -> int:
    c = read()
    return c.execute("PRAGMA freelist_count").fetchone()[0] * c.execute("PRAGMA page_size").fetchone()[0]


def _shrink():
    """Give deleted rows' space back to the file system (DELETE alone only marks pages free) and fold the log in."""
    c = _connect()
    if c.in_transaction:
        return
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:  # another writer or a long reader holds it: the next purge tries again
        pass


def size_limit_bytes() -> int:
    """The size jobs.db is kept under: Settings > "Database size limit" (MB), 50 MB until you change it."""
    try:
        mb = int(get_settings().get("max_db_mb") or 0)
    except (TypeError, ValueError):
        mb = 0
    return mb * 1024 * 1024 if mb >= 10 else MAX_DB_BYTES


def enforce_size_limit(max_bytes: int | None = None) -> int:
    """Keep jobs.db under max_bytes (default: size_limit_bytes()) so the app stays quick. Returns the number of
    postings removed.

    Order of what goes, stopping as soon as it fits: free space from earlier deletes; the oldest untouched postings
    (jobs you worked on are never deleted); run logs; the raw HTML of the oldest worked-on postings (their plain
    text stays and is what the page falls back to)."""
    max_bytes = max_bytes or size_limit_bytes()
    if _free_bytes() >= max(1024 * 1024, db_bytes() // 5):  # a fifth of the file is deleted rows: give it back
        _shrink()
    size = db_bytes()
    if size <= max_bytes:
        return 0
    _shrink()
    size = db_bytes()
    if size <= max_bytes:
        return 0
    excess = size - int(max_bytes * SIZE_HEADROOM)
    weight = "COALESCE(LENGTH(CAST(description_html AS BLOB)), 0) + COALESCE(LENGTH(CAST(description_text AS BLOB)), 0)"
    removed = 0
    with conn() as c:
        ids, freed = [], 0
        for r in c.execute(f"SELECT id, {weight} + {ROW_OVERHEAD} FROM jobs WHERE {UNTOUCHED} "
                           "ORDER BY COALESCE(posted_date, ''), COALESCE(first_seen, '')"):
            if freed >= excess:
                break
            ids.append(r[0])
            freed += r[1]
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            c.execute(f"DELETE FROM jobs WHERE id IN ({', '.join('?' * len(batch))})", batch)
        removed = len(ids)
        _drop_orphans(c)
        if freed < excess:  # everything left is yours: trim bulk that can be rebuilt
            freed += c.execute("SELECT COALESCE(SUM(LENGTH(CAST(log_text AS BLOB))), 0) FROM search_runs").fetchone()[0]
            c.execute("UPDATE search_runs SET log_text = ''")
        if freed < excess:
            rows = c.execute("SELECT id, LENGTH(CAST(description_html AS BLOB)) FROM jobs "
                             "WHERE description_html IS NOT NULL AND description_html <> '' "
                             "ORDER BY COALESCE(posted_date, ''), COALESCE(first_seen, '')").fetchall()
            for r in rows:
                if freed >= excess:
                    break
                c.execute("UPDATE jobs SET description_html = '' WHERE id = ?", (r[0],))
                freed += r[1]
    _shrink()
    return removed


# ---------- settings ----------
DEFAULT_SETTINGS = {
    "mandatory": "",
    "optional": "",
    "omit": "",
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
        "hide_knockouts": False,  # hide postings with a hard requirement you don't meet (sponsorship, degree...)
        "sponsorship": "any",     # any | offered (sponsors / H-1B) | h4ead | not_denied (drop "no sponsorship")
        "sort": "match_salary",   # list order, see SORTS
    },
    "profile": {
        "first_name": "", "last_name": "", "email": "", "phone": "", "phone_type": "Mobile",
        "address1": "", "address2": "", "city": "", "state": "", "postal_code": "", "country": "United States of America",
        "linkedin": "", "website": "", "github": "",
        "authorized_us": "Yes", "needs_sponsorship": "No", "previously_employed": "No",
        "how_heard": "Company Website",
        "us_citizen": "", "has_clearance": "",  # "", "Yes" or "No": blank never rules a posting out
    },
    # auto = bundled Qwen on CPU, GPU when this browser has a usable one; "onnx" = CPU only;
    # "ollama" / "openai" / "anthropic" = an external model (the bundled one is then only the fallback)
    "engine": "auto",
    "llm_url": "",              # external engine's address; empty = that engine's usual one (see llm.DEFAULT_URLS)
    "llm_model": "",            # the model name to ask that engine for
    "upload_format": "docx",
    "max_bullets": 12,
    "keep_new_days": RETENTION_DAYS,  # untouched postings expire after this (and a search looks back this far)
    "max_db_mb": MAX_DB_BYTES // (1024 * 1024),  # jobs.db is kept under this; the oldest untouched postings go first
    "ghost_after_days": 21,     # suggest "Ghosted" when an application has had no update for this long
    "last_visit": None,         # when the page was last opened ("new since your last visit")
    "scoring_version": 1,       # scoring.VERSION the stored match scores were computed with
    "autofill": {
        "experience": True,     # fill Workday's My Experience entries from master_resume.yaml
        "add_entries": False,   # ...and click its "Add" buttons for more entries (the one click autofill may make)
        "answers": True,        # reuse answers from the answer bank (filled for review, outlined amber)
        "capture": True,        # remember answers you type into custom questions
    },
    # Voluntary disclosures (EEO self-identification): "decline" picks "I don't wish to answer"; "skip" leaves it.
    "disclosures": {"gender": "decline", "ethnicity": "decline", "veteran": "decline", "disability": "decline"},
}


def get_settings() -> dict:
    rows = read().execute("SELECT key, value FROM settings").fetchall()
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
    r = read().execute("SELECT * FROM resume WHERE id = 1").fetchone()
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
    r = read().execute("SELECT last_success FROM company_runs WHERE company_key = ? AND sig = ?",
                       (company_key, sig)).fetchone()
    return datetime.fromisoformat(r["last_success"]) if r else None


def mark_success(company_key, sig, when: datetime):
    with conn() as c:
        c.execute("INSERT INTO company_runs(company_key, sig, last_success) VALUES(?, ?, ?) "
                  "ON CONFLICT(company_key, sig) DO UPDATE SET last_success = excluded.last_success",
                  (company_key, sig, when.isoformat(timespec="seconds")))


def last_run_overall():
    r = read().execute("SELECT MAX(last_success) AS m FROM company_runs").fetchone()
    return r["m"] if r else None


# ---------- jobs ----------
JSON_COLS = ("locations_json", "states_json", "matched_json", "missing_json", "optional_hits_json", "knockouts_json")
# What the job list needs: everything but the (large) description columns.
LIST_COLS = ("id", "company", "company_key", "title", "url", "location", "locations_json", "states_json",
             "remote_type", "employment_type", "worker_sub_type", "time_type", "salary_min", "salary_max",
             "salary_text", "posted_date", "match_score", "hidden", "status", "folder", "optional_hits_json",
             "first_seen", "knockouts_json", "feedback", "source", "sponsorship")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def job_exists(job_id) -> bool:
    return read().execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None


def job_ids(company_key) -> set:
    """IDs of the stored postings of one company (one query instead of one per posting during a search)."""
    return {r[0] for r in read().execute("SELECT id FROM jobs WHERE company_key = ?", (company_key,))}


def count_jobs() -> int:
    return read().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def touch_job(job_id):
    touch_jobs([job_id])


def touch_jobs(ids):
    now = _now()
    with conn() as c:
        c.executemany("UPDATE jobs SET last_seen = ? WHERE id = ?", [(now, i) for i in ids])


def upsert_job(job: dict):
    upsert_jobs([job])


def upsert_jobs(jobs):
    """Insert or refresh postings in one transaction. Your own fields (hidden/status/folder/first_seen) are kept."""
    now = _now()
    keep = {"hidden", "status", "folder", "first_seen"}
    with conn() as c:
        for job in jobs:
            row = dict(job)
            for k in JSON_COLS:
                if k in row and not isinstance(row[k], str):
                    row[k] = json.dumps(row[k])
            row.setdefault("first_seen", now)
            row["last_seen"] = now
            cols = list(row.keys())
            updates = ", ".join(f"{k} = excluded.{k}" for k in cols if k not in keep and k != "id")
            c.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)}) "
                      f"ON CONFLICT(id) DO UPDATE SET {updates}", [row[k] for k in cols])


def _decode(r) -> dict:
    d = dict(r)
    for k in JSON_COLS:
        if k in d:
            d[k.replace("_json", "")] = json.loads(d.pop(k) or "[]")
    return d


def _json_list(v):
    return [] if not v or v == "[]" else json.loads(v)


def _list_rows(sql, params):
    """Rows for the job list as dicts, decoded with plain tuples (thousands of rows: this is the hot path)."""
    cur = read().cursor()
    cur.row_factory = None
    cur.execute(sql, params)
    names = [d[0] for d in cur.description]
    keys = [n[:-5] if n in JSON_COLS else n for n in names]
    json_at = [i for i, n in enumerate(names) if n in JSON_COLS]
    out = []
    for row in cur.fetchall():
        d = dict(zip(keys, row))
        for i in json_at:
            d[keys[i]] = _json_list(row[i])
        out.append(d)
    return out


def all_jobs(with_text=False):
    cols = "*" if with_text else ", ".join(LIST_COLS)
    return [_decode(r) for r in read().execute(f"SELECT {cols} FROM jobs").fetchall()]


PREPARING = ("tailored", "saved", "applying")  # statuses still being worked on before applying


_MATCH = "COALESCE(match_score, -1) DESC"   # unscored last
_SALARY = "COALESCE(salary_max, 0) DESC"    # unlisted last
_DATE = "COALESCE(posted_date, '') DESC"    # newest first
SORTS = {
    "match_salary": (_MATCH, _SALARY),
    "match_date": (_MATCH, _DATE),
    "date_salary": (_DATE, _SALARY),
    "date_match": (_DATE, _MATCH),
    "salary_date": (_SALARY, _DATE),
    "salary_match": (_SALARY, _MATCH),
    "date_oldest": ("COALESCE(posted_date, '9999') ASC", _MATCH),
}


def jobs_for_list(types, show_hidden=False, remote_only=False, min_salary=0, include_no_salary=True,
                  kw_sig=None, posted_since=None, hide_knockouts=False, sponsorship="any",
                  sort="match_salary"):
    """The job list's cheap filters, done in SQL. kw_sig: keep only jobs whose stored keyword check (for this
    keyword set) passed the mandatory keywords. posted_since: older postings are listed only while you are still
    preparing them (applied ones live on the pipeline board)."""
    where, params = [f"employment_type IN ({', '.join('?' for _ in types)})"], list(types)
    if posted_since:
        where.append(f"(posted_date >= ? OR status IN ({', '.join('?' for _ in PREPARING)}))")
        params += [posted_since, *PREPARING]
    if not show_hidden:
        where.append("COALESCE(hidden, 0) = 0")
    if remote_only:
        where.append("remote_type = 'Remote'")
    if hide_knockouts:
        where.append("(knockouts_json IS NULL OR knockouts_json = '[]')")
    tagged = "',' || COALESCE(sponsorship, '') || ','"
    if sponsorship == "offered":
        where.append(f"({tagged} LIKE '%,sponsors,%' OR {tagged} LIKE '%,h1b,%')")
    elif sponsorship == "h4ead":
        where.append(f"{tagged} LIKE '%,h4ead,%'")
    elif sponsorship == "not_denied":
        where.append(f"{tagged} NOT LIKE '%,no,%'")
    if not include_no_salary:
        where.append("salary_max IS NOT NULL")
    if min_salary:
        where.append("(salary_max IS NULL OR salary_max >= ?)")
        params.append(min_salary)
    if kw_sig is not None:
        where.append("kw_sig = ? AND mandatory_ok = 1")
        params.append(kw_sig)
    order = ", ".join(SORTS.get(sort) or SORTS["match_salary"])
    sql = (f"SELECT {', '.join(LIST_COLS)} FROM jobs WHERE {' AND '.join(where)} ORDER BY {order}, id")
    return _list_rows(sql, params)


def jobs_needing_keywords(kw_sig):
    """(id, title, description_text) of jobs whose stored keyword check is for a different keyword set."""
    c = read()
    # The kw_sig index answers "is anything stale?" without reading the large job rows.
    stale = [r[0] for r in c.execute("SELECT rowid FROM jobs INDEXED BY jobs_kw WHERE kw_sig IS NOT ?", (kw_sig,))]
    if not stale:
        return []
    if len(stale) > 500:
        return c.execute("SELECT id, title, description_text FROM jobs WHERE kw_sig IS NOT ?", (kw_sig,)).fetchall()
    return c.execute(f"SELECT id, title, description_text FROM jobs WHERE rowid IN ({', '.join('?' * len(stale))})",
                     stale).fetchall()


def set_keyword_hits(kw_sig, hits):
    """hits: [(job_id, mandatory_ok, optional_hits)]; mandatory_ok is also False for a posting with an omit word."""
    with conn() as c:
        c.executemany("UPDATE jobs SET kw_sig = ?, mandatory_ok = ?, optional_hits_json = ? WHERE id = ?",
                      [(kw_sig, int(ok), json.dumps(opt), job_id) for job_id, ok, opt in hits])


def set_sponsorship(rows):
    """rows: [(job_id, tags)] where tags is a list like ["h1b", "h4ead"]."""
    with conn() as c:
        c.executemany("UPDATE jobs SET sponsorship = ? WHERE id = ?", [(",".join(t), i) for i, t in rows])


def set_scores(scores):
    """scores: [(job_id, match_score, matched, missing, knockouts)] in one transaction."""
    with conn() as c:
        c.executemany("UPDATE jobs SET match_score = ?, matched_json = ?, missing_json = ?, knockouts_json = ? "
                      "WHERE id = ?", [(s, json.dumps(m), json.dumps(x), json.dumps(k), job_id)
                                       for job_id, s, m, x, k in scores])


def get_job(job_id):
    r = read().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
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
    r = read().execute("SELECT * FROM tailored WHERE job_id = ?", (job_id,)).fetchone()
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
                  (job_id, json.dumps(doc), score_after, json.dumps(approved), json.dumps(rejected), _now()))


# ---------- pipeline ----------
PIPELINE_COLS = (*LIST_COLS, "status_at", "next_action_at", "next_action", "closed_at", "req_id")


def tracked_jobs():
    """Every job past 'new' (the pipeline board), with its tailored match score when there is one."""
    cols = ", ".join(f"j.{c}" for c in PIPELINE_COLS)
    return _list_rows(f"SELECT {cols}, t.score_after FROM jobs j LEFT JOIN tailored t ON t.job_id = j.id "
                      "WHERE COALESCE(j.status, 'new') <> 'new'", ())


def set_status(job_id, status, at):
    with conn() as c:
        c.execute("UPDATE jobs SET status = ?, status_at = ? WHERE id = ?", (status, at, job_id))


def add_event(job_id, kind, note=None, from_status=None, to_status=None, at=None):
    with conn() as c:
        c.execute("INSERT INTO job_events(job_id, at, kind, from_status, to_status, note) VALUES(?, ?, ?, ?, ?, ?)",
                  (job_id, at or _now(), kind, from_status, to_status, note))


def job_events(job_id):
    rows = read().execute("SELECT at, kind, from_status, to_status, note FROM job_events WHERE job_id = ? "
                          "ORDER BY at, id", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def status_events():
    """(job_id, to_status, at) of every status change, oldest first (pipeline statistics)."""
    return read().execute("SELECT job_id, to_status, at FROM job_events WHERE kind = 'status' "
                          "ORDER BY at, id").fetchall()


def set_follow_up(job_id, at, action):
    with conn() as c:
        c.execute("UPDATE jobs SET next_action_at = ?, next_action = ? WHERE id = ?", (at, action, job_id))


def tracked_to_check(company_key, statuses, checked_before, limit=25):
    """Tracked, still-open postings of one company not checked since checked_before (is it still posted?)."""
    marks = ", ".join("?" for _ in statuses)
    return read().execute(f"SELECT id, title, external_path FROM jobs WHERE company_key = ? AND status IN ({marks}) "
                          "AND closed_at IS NULL AND external_path IS NOT NULL "
                          "AND (checked_at IS NULL OR checked_at < ?) ORDER BY checked_at LIMIT ?",
                          (company_key, *statuses, checked_before, limit)).fetchall()


def set_checked(job_id, at, closed=False):
    with conn() as c:
        if closed:
            c.execute("UPDATE jobs SET checked_at = ?, closed_at = ? WHERE id = ?", (at, at, job_id))
        else:
            c.execute("UPDATE jobs SET checked_at = ? WHERE id = ?", (at, job_id))


# ---------- saved searches, run history, alerts ----------
SEARCH_FIELDS = ("name", "mandatory", "optional", "omit", "every_hours", "notify_min_score", "enabled")


def saved_searches():
    rows = read().execute("SELECT * FROM saved_searches ORDER BY name COLLATE NOCASE, id").fetchall()
    return [dict(r) for r in rows]


def get_saved_search(search_id):
    r = read().execute("SELECT * FROM saved_searches WHERE id = ?", (search_id,)).fetchone()
    return dict(r) if r else None


def create_saved_search(values: dict) -> int:
    cols = [k for k in SEARCH_FIELDS if k in values]
    with conn() as c:
        cur = c.execute(f"INSERT INTO saved_searches({', '.join(cols)}, created_at) "
                        f"VALUES({', '.join('?' for _ in cols)}, ?)", [values[k] for k in cols] + [_now()])
        return cur.lastrowid


def update_saved_search(search_id, values: dict):
    cols = [k for k in SEARCH_FIELDS if k in values]
    if cols:
        with conn() as c:
            c.execute(f"UPDATE saved_searches SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                      [values[k] for k in cols] + [search_id])


def delete_saved_search(search_id):
    with conn() as c:
        c.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        c.execute("DELETE FROM alerts WHERE search_id = ?", (search_id,))


def mark_search_ran(search_id, at):
    with conn() as c:
        c.execute("UPDATE saved_searches SET last_run_at = ? WHERE id = ?", (at, search_id))


def record_run(run: dict) -> int:
    with conn() as c:
        cur = c.execute("INSERT INTO search_runs(search_id, search_name, mode, trigger, started, finished, new_jobs, "
                        "refreshed, rejected, errors_json, log_text) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run.get("search_id"), run.get("search_name"), run.get("mode"), run.get("trigger"),
                         run.get("started"), run.get("finished"), run.get("new_jobs", 0), run.get("refreshed", 0),
                         run.get("rejected", 0), json.dumps(run.get("errors") or []), run.get("log_text", "")))
        return cur.lastrowid


def search_runs(limit=30):
    rows = read().execute("SELECT * FROM search_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["errors"] = json.loads(d.pop("errors_json") or "[]")
        out.append(d)
    return out


def add_alerts(rows):
    """rows: [(job_id, search_id, score)]; a job already alerted for that search is not alerted again."""
    now = _now()
    with conn() as c:
        cur = c.executemany("INSERT OR IGNORE INTO alerts(job_id, search_id, score, created_at) VALUES(?, ?, ?, ?)",
                            [(j, s, sc, now) for j, s, sc in rows])
        return cur.rowcount


def alerts(unseen_only=True, since=None, limit=200):
    where, params = [], []
    if unseen_only:
        where.append("a.seen = 0")
    if since:
        where.append("a.created_at >= ?")
        params.append(since)
    sql = ("SELECT a.id, a.job_id, a.search_id, a.score, a.created_at, a.seen, s.name AS search_name, "
           "j.title, j.company, j.location, j.url, j.salary_min, j.salary_max FROM alerts a "
           "JOIN jobs j ON j.id = a.job_id LEFT JOIN saved_searches s ON s.id = a.search_id "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY a.score DESC, a.id DESC LIMIT ?")
    return [dict(r) for r in read().execute(sql, (*params, limit)).fetchall()]


def mark_alerts_seen(ids=None):
    with conn() as c:
        if ids is None:
            c.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")
        else:
            c.executemany("UPDATE alerts SET seen = 1 WHERE id = ?", [(i,) for i in ids])


# ---------- answer bank ----------
ANSWER_FIELDS = ("question", "answer", "kind", "source", "company")


def answers_all():
    return [dict(r) for r in read().execute("SELECT * FROM answers ORDER BY updated_at DESC, id DESC").fetchall()]


def answer_get(answer_id):
    r = read().execute("SELECT * FROM answers WHERE id = ?", (answer_id,)).fetchone()
    return dict(r) if r else None


def answer_upsert(question, norm, answer, kind="text", source="manual", company=None) -> int:
    """Insert, or replace the answer of the question with the same normalized text. Returns its id."""
    now = _now()
    with conn() as c:
        c.execute("INSERT INTO answers(question, norm, answer, kind, source, company, created_at, updated_at) "
                  "VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(norm) DO UPDATE SET question = excluded.question, "
                  "answer = excluded.answer, kind = excluded.kind, source = excluded.source, "
                  "company = excluded.company, updated_at = excluded.updated_at",
                  (question, norm, answer, kind, source, company, now, now))
        return c.execute("SELECT id FROM answers WHERE norm = ?", (norm,)).fetchone()[0]


def answer_update(answer_id, **fields):
    fields = {k: v for k, v in fields.items() if k in ANSWER_FIELDS + ("norm",)}
    if not fields:
        return
    fields["updated_at"] = _now()
    with conn() as c:
        c.execute(f"UPDATE answers SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                  (*fields.values(), answer_id))


def answer_delete(answer_id) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM answers WHERE id = ?", (answer_id,)).rowcount > 0


def answers_used(ids):
    now = _now()
    with conn() as c:
        c.executemany("UPDATE answers SET uses = uses + 1, last_used_at = ? WHERE id = ?", [(now, i) for i in ids])
