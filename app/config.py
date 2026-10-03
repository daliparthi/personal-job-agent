"""
Paths and constants.

The source folder (this project) is shared and never holds personal data, so one copy of the code can serve
several people on the same computer (Windows, macOS or Linux). Each person gets a personal folder:

    <your home folder>/JobAgent/<profile>/    e.g. C:\\Users\\you\\JobAgent\\default, /Users/you/JobAgent/default,
                                              /home/you/JobAgent/default; profile "default" unless --profile NAME
        .env                 Workday account email + password (fills Create Account / Sign In forms)
        my_companies.yaml    your own Workday sites, on top of the shared companies.yaml in the program folder
        master_resume.yaml   your master resume, parsed by the local model (edit freely)
        jobs.db              settings, postings (new ones expire after 7 days; ones you worked on are kept), tailored resumes
        browser-profile/     the apply window's own browser profile (Workday logins)
        Applications/        saved application packages

run.py sets JOB_AGENT_HOME from --profile / --home before this module is imported.
"""
import hashlib
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
MODELS = ROOT / "models"
COMPANIES_SHARED = ROOT / "companies.yaml"  # shared list, read fresh for every search

PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}")


def home_for(profile=None, home=None) -> Path:
    if home:
        return Path(home).expanduser().resolve()
    name = (profile or "default").strip()
    if not PROFILE_RE.fullmatch(name) or name.endswith((".", " ")):
        raise ValueError("Profile names may use letters, numbers, spaces, '.', '_' and '-' (max 40)")
    return Path.home() / "JobAgent" / name


HOME = home_for(os.environ.get("JOB_AGENT_PROFILE"), os.environ.get("JOB_AGENT_HOME"))
HOME_ID = hashlib.sha1(str(HOME).lower().encode()).hexdigest()[:16]  # names this personal folder (ping, keychain)
DB_PATH = HOME / "jobs.db"
BROWSER_PROFILE = HOME / "browser-profile"
APPLICATIONS = HOME / "Applications"
MY_COMPANIES = HOME / "my_companies.yaml"
OLD_COMPANIES_COPY = HOME / "companies.yaml"  # stale copy made by earlier versions; migrated away
MASTER_YAML = HOME / "master_resume.yaml"
ENV_FILE = HOME / ".env"
KEYCHAIN_INDEX = HOME / ".keychain-entries"  # names (never values) of the OS keychain entries this folder made
SESSION_KEY_FILE = HOME / ".session-key"
PORT_FILE = HOME / ".port"

HOST = "127.0.0.1"
PORT = int(os.environ.get("JOB_AGENT_PORT") or 8765)

RETENTION_DAYS = 7          # untouched postings older than this are purged; first runs look back this far
MAX_PAGES_PER_QUERY = 50    # 50 x 20 = 1000 postings per company/job-type before we stop paging
COMPANY_CONCURRENCY = 3     # companies searched in parallel
DETAIL_CONCURRENCY = 4      # job-detail requests in flight per company


def _private(path: Path):
    """macOS/Linux home folders can be readable by other users: keep the personal folder to its owner.
    (On Windows the user profile folder is already private.)"""
    if os.name == "posix" and path.exists():
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass


def ensure_home():
    """Create the personal folder and seed it on first run."""
    from .envfile import TEMPLATE as ENV_TEMPLATE

    HOME.mkdir(parents=True, exist_ok=True)
    _private(HOME)
    APPLICATIONS.mkdir(exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
    _private(ENV_FILE)


def session_key() -> str:
    """Per-person secret: the local server only answers browsers that opened it through run.py's link."""
    if SESSION_KEY_FILE.exists():
        key = SESSION_KEY_FILE.read_text(encoding="utf-8").strip()
        if len(key) >= 20:
            return key
    key = secrets.token_urlsafe(24)
    SESSION_KEY_FILE.write_text(key, encoding="utf-8")
    _private(SESSION_KEY_FILE)
    return key
