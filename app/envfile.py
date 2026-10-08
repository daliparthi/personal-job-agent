"""Your Workday account: the email/password used to fill Create Account and Sign In forms.

The email lives in the personal .env file. The password can live in the OS keychain (Windows Credential Manager,
macOS Keychain, Linux Secret Service) or in .env. For one company the lookup order is: that company's keychain
entry, its .env line, then the general keychain entry, then the general .env line.

A password is read only when an apply window opens and is typed by Playwright into the password fields of the
posting's own Workday site. It is never sent to the Job Agent page, logged, or written anywhere else; the page can
only hand a new password to this computer's keychain (Settings > Workday account).
"""
import re

from .config import ENV_FILE, HOME_ID, KEYCHAIN_INDEX, _private

TEMPLATE = """\
# Job Agent - your Workday account. This file stays on this computer, in your personal folder.
#
# Every company runs its own Workday site, so you create one Workday account per company.
# When an apply window reaches Workday's "Create Account" or "Sign In" form, Job Agent types this
# email and password into it. You tick any terms box and click "Create Account" / "Sign In" yourself.
# Safer: leave WORKDAY_PASSWORD empty and store the password in your OS keychain instead
# (Job Agent: Settings > Workday account).
WORKDAY_EMAIL=
WORKDAY_PASSWORD=

# Optional: a different email/password for one company. Use the first part of its Workday address,
# e.g. https://nvidia.wd5.myworkdayjobs.com/... -> NVIDIA
# NVIDIA_WORKDAY_EMAIL=
# NVIDIA_WORKDAY_PASSWORD=
"""

PASSWORD_KEY_RE = re.compile(r"(?:[A-Z0-9_]+_)?WORKDAY_PASSWORD")
COMPANY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}")


def _value(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        q = raw[0]
        end = raw.find(q, 1)
        while q == '"' and end > 0 and raw[end - 1] == "\\":
            end = raw.find(q, end + 1)
        inner = raw[1:end] if end > 0 else raw[1:]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if q == '"' else inner
    # Unquoted: " #" starts a comment, a bare "#" is part of the value (passwords often contain one).
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def _parse_line(line: str):
    """(KEY, value) for a KEY=value line, else None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    key, sep, val = line.partition("=")
    if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
        return key.strip().upper(), _value(val)
    return None


def read(path=ENV_FILE) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        kv = _parse_line(line)
        if kv:
            out[kv[0]] = kv[1]
    return out


def tenant_prefix(tenant: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", (tenant or "").upper())


def password_key(company: str = "") -> str:
    """The .env / keychain name of a password: WORKDAY_PASSWORD, or NVIDIA_WORKDAY_PASSWORD for one company."""
    return f"{tenant_prefix(company)}_WORKDAY_PASSWORD" if company else "WORKDAY_PASSWORD"


# ---------------------------------------------------------------- OS keychain
SERVICE = f"job-agent/{HOME_ID}"  # one keychain "service" per personal folder (profiles never see each other's)


def _keyring():
    """The keyring module when this computer has a real OS keychain, else None (then .env is used)."""
    try:
        import keyring
        backend = keyring.get_keyring()
    except Exception:
        return None
    return keyring if getattr(backend, "priority", 0) > 0 else None


def keychain_available() -> bool:
    return _keyring() is not None


def _keychain_get(name: str) -> str:
    kr = _keyring()
    if not kr:
        return ""
    try:
        return kr.get_password(SERVICE, name) or ""
    except Exception:
        return ""


def _index() -> list:
    if not KEYCHAIN_INDEX.exists():
        return []
    return [n for n in KEYCHAIN_INDEX.read_text(encoding="utf-8").split() if PASSWORD_KEY_RE.fullmatch(n)]


def _write_index(names):
    KEYCHAIN_INDEX.write_text("".join(f"{n}\n" for n in sorted(set(names))), encoding="utf-8")
    _private(KEYCHAIN_INDEX)


def store_password(password: str, company: str = "") -> str:
    """Save a password in the OS keychain (an empty one deletes it). Returns its name."""
    kr = _keyring()
    if not kr:
        raise RuntimeError("This computer has no OS keychain Job Agent can use; keep the password in the .env file.")
    if company and not COMPANY_RE.fullmatch(company):
        raise ValueError("Company: use the first part of its Workday address, e.g. NVIDIA")
    name = password_key(company)
    names = set(_index())
    if password:
        kr.set_password(SERVICE, name, password)
        names.add(name)
    else:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:
            pass  # nothing stored
        names.discard(name)
    _write_index(names)
    return name


def move_env_passwords_to_keychain() -> list:
    """Copy every password in .env into the keychain, then blank it in .env. Returns the names moved."""
    kr = _keyring()
    if not kr:
        raise RuntimeError("This computer has no OS keychain Job Agent can use; keep the password in the .env file.")
    env = read()
    moved = [k for k, v in env.items() if v and PASSWORD_KEY_RE.fullmatch(k)]
    for k in moved:
        kr.set_password(SERVICE, k, env[k])
    if moved:
        _write_index(set(_index()) | set(moved))
        _blank_env_values(moved)
    return moved


def _blank_env_values(keys):
    """Empty those KEY=value lines in .env (comments and every other line stay exactly as they were)."""
    keys = set(keys)
    with open(ENV_FILE, encoding="utf-8-sig", newline="") as f:  # newline="": keep the file's own line endings
        raw = f.read()
    out = []
    for line in raw.splitlines(keepends=True):
        kv = _parse_line(line)
        if kv and kv[0] in keys:
            ending = line[len(line.rstrip("\r\n")):]
            indent = line[:len(line) - len(line.lstrip())]
            export = "export " if line.strip().startswith("export ") else ""
            out.append(f"{indent}# {kv[0]} is stored in the OS keychain (Job Agent: Settings > Workday account){ending}")
            out.append(f"{indent}{export}{kv[0]}={ending}")
        else:
            out.append(line)
    with open(ENV_FILE, "w", encoding="utf-8", newline="") as f:
        f.write("".join(out))
    _private(ENV_FILE)


# ---------------------------------------------------------------- API keys for external AI engines
API_KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def api_key(engine: str) -> str:
    """The key for an external AI engine: the OS keychain first, then .env. Read only by the server."""
    name = API_KEY_NAMES.get(engine)
    if not name:
        return ""
    return (_keychain_get(name) or read().get(name) or "").strip()


def store_api_key(engine: str, key: str):
    """Save an engine's API key in the OS keychain (an empty key deletes it)."""
    name = API_KEY_NAMES.get(engine)
    if not name:
        raise ValueError("This engine needs no API key")
    kr = _keyring()
    if not kr:
        raise RuntimeError(f"This computer has no OS keychain Job Agent can use; put {name}=... in the .env file.")
    if key:
        kr.set_password(SERVICE, name, key)
    else:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:
            pass  # nothing stored


def api_keys_set() -> dict:
    return {engine: bool(api_key(engine)) for engine in API_KEY_NAMES}


# ---------------------------------------------------------------- lookups
def account_for(tenant: str) -> dict:
    env = read()
    company = password_key(tenant) if tenant else None
    password = ""
    for name in ([company] if company else []) + ["WORKDAY_PASSWORD"]:
        password = _keychain_get(name) or env.get(name) or ""
        if password:
            break
    t = tenant_prefix(tenant)
    return {"email": (env.get(f"{t}_WORKDAY_EMAIL") or env.get("WORKDAY_EMAIL") or "").strip(), "password": password}


def status() -> dict:
    """What the Settings dialog shows. Never includes a password."""
    env = read()
    in_keychain = {n for n in _index() if _keychain_get(n)}
    in_env = {k for k, v in env.items() if v and PASSWORD_KEY_RE.fullmatch(k)}
    companies = {k.rsplit("_WORKDAY_", 1)[0] for k, v in env.items()
                 if v and re.fullmatch(r".+_WORKDAY_(EMAIL|PASSWORD)", k)}
    companies |= {n.rsplit("_WORKDAY_", 1)[0] for n in in_keychain if n != "WORKDAY_PASSWORD"}
    return {"path": str(ENV_FILE), "email": env.get("WORKDAY_EMAIL", ""),
            "password_set": "WORKDAY_PASSWORD" in in_env | in_keychain,
            "password_in_keychain": "WORKDAY_PASSWORD" in in_keychain,
            "password_in_env": "WORKDAY_PASSWORD" in in_env,
            "env_passwords": sorted(in_env), "keychain": keychain_available(),
            "company_overrides": sorted(companies), "api_keys": api_keys_set()}
