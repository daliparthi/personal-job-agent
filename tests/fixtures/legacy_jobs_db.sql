-- jobs.db exactly as Job Agent created it before schema migrations existed (user_version 0), with a year-old
-- applied job, a tailored resume and settings. tests/test_storage.py upgrades it and checks nothing was lost.
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
CREATE TABLE IF NOT EXISTS resume (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    filename TEXT, text TEXT, data TEXT, yaml_mtime REAL, uploaded_at TEXT
);

INSERT INTO jobs (id, company, company_key, tenant, site, title, url, external_path, req_id, location, locations_json,
                  states_json, country, remote_type, remote_raw, employment_type, worker_sub_type, time_type, salary_min,
                  salary_max, salary_text, posted_date, description_html, description_text, match_score, matched_json,
                  missing_json, hidden, status, folder, first_seen, last_seen)
VALUES
 ('acme/external:OLD1', 'Acme', 'acme/external', 'acme', 'External', 'Data Engineer',
  'https://acme.wd1.myworkdayjobs.com/External/job/x_OLD1', '/job/x_OLD1', 'OLD1', 'Austin, TX', '["Austin, TX"]',
  '["TX"]', 'United States', 'Hybrid', 'Hybrid', 'Full-time', 'Regular', 'Full time', 120000, 150000,
  '$120,000 - $150,000', '2025-09-15', '<p>Python and SQL</p>', 'Python and SQL', 61, '["Python", "SQL"]', '[]', 0,
  'applied', 'C:/Users/me/JobAgent/default/Applications/Acme/Data Engineer - OLD1', '2025-09-16T08:00:00',
  '2025-09-20T08:00:00'),
 ('acme/external:NEW1', 'Acme', 'acme/external', 'acme', 'External', 'Analytics Engineer',
  'https://acme.wd1.myworkdayjobs.com/External/job/x_NEW1', '/job/x_NEW1', 'NEW1', 'Remote', '["Remote"]', '[]',
  'United States', 'Remote', 'Remote', 'Full-time', 'Regular', 'Full time', NULL, NULL, '', '2099-01-01',
  '<p>dbt and SQL</p>', 'dbt and SQL', 40, '["SQL"]', '[{"keyword": "dbt"}]', 0, 'new', NULL,
  '2099-01-01T08:00:00', '2099-01-01T08:00:00');
INSERT INTO tailored VALUES ('acme/external:OLD1', '{"resume": {"name": "Jordan"}, "changes": {}}', 70, '["Spark"]', '[]',
                             '2025-09-16T09:00:00');
INSERT INTO settings VALUES ('mandatory', '"sql"'), ('filters', '{"remote_only": false, "types": ["Full-time"]}');
INSERT INTO company_runs VALUES ('acme/external', 'abc123', '2099-01-01T08:00:00');
INSERT INTO resume VALUES (1, 'cv.docx', 'Python SQL', '{"name": "Jordan", "sections": []}', 0, '2025-09-01T08:00:00');
