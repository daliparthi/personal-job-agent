"""Shared test setup.

app.config reads JOB_AGENT_HOME the first time it is imported and the other modules copy paths out of it, so the
environment must point at a throwaway personal folder before anything from app/ is imported. run.py does the same
(it sets JOB_AGENT_HOME, then reloads app.config).
"""
import os
import shutil
from datetime import date
import tempfile
from pathlib import Path

import pytest

HOME = Path(tempfile.mkdtemp(prefix="jobagent-tests-"))
os.environ["JOB_AGENT_HOME"] = str(HOME)
os.environ.pop("JOB_AGENT_PROFILE", None)
os.environ.pop("JOB_AGENT_PORT", None)

import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import PasswordDeleteError  # noqa: E402


class MemoryKeyring(KeyringBackend):
    """Stands in for the OS keychain so tests never read or write your real Credential Manager / Keychain."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError("not found")


KEYCHAIN = MemoryKeyring()
keyring.set_keyring(KEYCHAIN)

from app import config, db  # noqa: E402

assert config.HOME == HOME.resolve() or config.HOME == HOME, "app.config was imported before conftest.py"

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_RESUME = ROOT / "samples" / "sample_master_resume.txt"


def _release_db():
    """Let background re-scoring finish and close every SQLite connection (Windows can't delete an open file)."""
    from app import search
    search.rescorer.wait(30)
    db.close_all()


@pytest.fixture(autouse=True)
def fresh_home():
    """An empty personal folder and database for every test."""
    _release_db()
    KEYCHAIN.store.clear()
    for name in ("jobs.db", "jobs.db-wal", "jobs.db-shm", "my_companies.yaml", "master_resume.yaml", ".env",
                 "companies.yaml", "companies.yaml.old", ".keychain-entries"):
        (config.HOME / name).unlink(missing_ok=True)
    shutil.rmtree(config.APPLICATIONS, ignore_errors=True)
    shutil.rmtree(config.ALERTS, ignore_errors=True)
    config.ensure_home()
    db.init()
    yield config.HOME
    _release_db()


def pytest_sessionfinish(session, exitstatus):
    db.close_all()
    shutil.rmtree(HOME, ignore_errors=True)


# ---------------------------------------------------------------- helpers shared by several test files
def make_job(job_id="acme/external:R1", **over):
    """A jobs-table row as SearchRunner._detail would store it."""
    job = {
        "id": job_id, "company": "Acme", "company_key": "acme/external", "tenant": "acme", "site": "External",
        "title": "Data Engineer", "url": "https://acme.wd1.myworkdayjobs.com/External/job/Austin-TX/Data-Engineer_R1",
        "external_path": "/job/Austin-TX/Data-Engineer_R1", "req_id": "R1",
        "location": "Austin, TX", "locations_json": ["Austin, TX"], "states_json": ["TX"], "country": "United States",
        "remote_raw": "", "remote_type": "On-site", "employment_type": "Full-time", "worker_sub_type": "Regular",
        "time_type": "Full time", "salary_min": 120000.0, "salary_max": 150000.0, "salary_text": "$120,000 - $150,000",
        "posted_date": date.today().isoformat(), "description_html": "<p>We use Python and SQL on AWS.</p>",
        "description_text": "We use Python and SQL on AWS.",
    }
    job.update(over)
    return job


SAMPLE_MASTER = {
    "name": "Jordan Avery",
    "headline": "",
    "contact": {"email": "jordan.avery@example.com", "phone": "(555) 010-0199", "location": "Austin, TX",
                "links": ["linkedin.com/in/jordan-avery-example"]},
    "sections": [
        {"title": "Professional Summary", "kind": "summary",
         "text": "Data engineer with 8 years of experience building data platforms on AWS."},
        {"title": "Skills", "kind": "skills", "groups": [{"name": "Languages", "items": ["Python", "SQL"]},
                                                         {"name": "Data", "items": ["Apache Spark", "Airflow"]}]},
        {"title": "Experience", "kind": "experience", "entries": [
            {"title": "Senior Data Engineer", "company": "Northwind Analytics", "location": "Austin, TX",
             "start": "2021", "end": "Present",
             "bullets": ["Designed Spark and Airflow pipelines that process 3 TB of data per day.",
                         "Migrated a legacy warehouse to Snowflake, cutting cost by 35%."]},
        ]},
        {"title": "Education", "kind": "education", "entries": [
            {"degree": "B.S. Computer Science", "school": "University of Texas at Austin", "end": "2016"}]},
    ],
}
