"""The personal .env file: the Workday account email/password used to fill Create Account and Sign In forms.

The password is read only when an apply window opens, is typed by Playwright into the password fields of the
posting's own Workday site, and is never sent to the Job Agent page, logged, or written anywhere else.
"""
import re

from .config import ENV_FILE

TEMPLATE = """\
# Job Agent - your Workday account. This file stays on this computer, in your personal folder.
#
# Every company runs its own Workday site, so you create one Workday account per company.
# When an apply window reaches Workday's "Create Account" or "Sign In" form, Job Agent types this
# email and password into it. You tick any terms box and click "Create Account" / "Sign In" yourself.
WORKDAY_EMAIL=
WORKDAY_PASSWORD=

# Optional: a different email/password for one company. Use the first part of its Workday address,
# e.g. https://nvidia.wd5.myworkdayjobs.com/... -> NVIDIA
# NVIDIA_WORKDAY_EMAIL=
# NVIDIA_WORKDAY_PASSWORD=
"""


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


def read(path=ENV_FILE) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            out[key.strip().upper()] = _value(val)
    return out


def tenant_prefix(tenant: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", (tenant or "").upper())


def account_for(tenant: str) -> dict:
    env = read()
    t = tenant_prefix(tenant)
    return {"email": (env.get(f"{t}_WORKDAY_EMAIL") or env.get("WORKDAY_EMAIL") or "").strip(),
            "password": env.get(f"{t}_WORKDAY_PASSWORD") or env.get("WORKDAY_PASSWORD") or ""}


def status() -> dict:
    """What the Settings dialog shows. Never includes a password."""
    env = read()
    companies = sorted({k.rsplit("_WORKDAY_", 1)[0] for k, v in env.items()
                        if v and re.fullmatch(r".+_WORKDAY_(EMAIL|PASSWORD)", k)})
    return {"path": str(ENV_FILE), "email": env.get("WORKDAY_EMAIL", ""),
            "password_set": bool(env.get("WORKDAY_PASSWORD")), "company_overrides": companies}
