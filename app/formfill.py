"""What the apply window's autofill (autofill.js) gets: your profile, the field rules (autofill_rules.json), your work
history and education from master_resume.yaml, your voluntary-disclosure choices and the autofill settings."""
import json
import re
from pathlib import Path

from .candidate import parse_when
from .jobparse import degree_level

RULES_PATH = Path(__file__).parent / "autofill_rules.json"

# Workday's degree list varies by company; the first of these that an option starts with is picked.
DEGREE_OPTIONS = {
    1: ["Bachelor's Degree", "Bachelors Degree", "Bachelor's", "Bachelors", "Bachelor", "BS", "BA"],
    2: ["Master's Degree", "Masters Degree", "Master's", "Masters", "Master", "MBA", "MS", "MA"],
    3: ["Doctorate", "Doctoral Degree", "PhD", "Ph.D"],
}
_DEGREE_PREFIX = re.compile(
    r"^(?:bachelor'?s?|master'?s?|associate'?s?|doctor(?:ate)?|ph\.?\s?d\.?|mba|[bm]\.?\s?(?:s|a|sc|eng)\.?(?![a-z]))"
    r"(?:\s+of\s+(?:science|arts|engineering|business administration|technology))?(?:\s+degree)?(?:\s+in)?[\s,:-]*",
    re.I)
_CURRENT = re.compile(r"^(present|current|now|today|ongoing)", re.I)
_HAS_MONTH = re.compile(r"\b\d{1,2}/(19|20)\d{2}\b|\b[a-z]{3,9}\.?,?\s+(19|20)\d{2}\b", re.I)


def load_rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _when(text, end=False):
    """(year, month or None, current) from a resume date. The month is given only when the resume names one."""
    t = (text or "").strip()
    if _CURRENT.match(t):
        return None, None, True
    ym = parse_when(t, end)
    if not ym:
        return None, None, False
    return ym[0], (ym[1] if _HAS_MONTH.search(t) else None), False


def experience(master: dict) -> list:
    out = []
    for s in (master or {}).get("sections") or []:
        if s.get("kind") != "experience":
            continue
        for e in s.get("entries") or []:
            if not (e.get("title") or e.get("company")):
                continue
            sy, sm, _ = _when(e.get("start"))
            ey, em, current = _when(e.get("end"), end=True)
            bullets = [b for b in e.get("bullets") or [] if b]
            out.append({
                "title": e.get("title") or "", "company": e.get("company") or "", "location": e.get("location") or "",
                "start_year": sy, "start_month": sm, "end_year": ey, "end_month": em, "current": current,
                "description": ("\n".join(f"• {b}" for b in bullets) or e.get("description") or "")[:4000],
            })
    return out[:10]


def education(master: dict) -> list:
    out = []
    for s in (master or {}).get("sections") or []:
        if s.get("kind") != "education":
            continue
        for e in s.get("entries") or []:
            if not (e.get("school") or e.get("degree")):
                continue
            degree = (e.get("degree") or "").strip()
            sy, _, _ = _when(e.get("start"))
            ey, _, _ = _when(e.get("end"), end=True)
            out.append({
                "school": e.get("school") or "", "degree": degree,
                "degree_options": DEGREE_OPTIONS.get(degree_level(degree), []),
                "field": _DEGREE_PREFIX.sub("", degree).strip(" ,-") if _DEGREE_PREFIX.match(degree) else "",
                "gpa": str(e.get("gpa") or ""), "start_year": sy, "end_year": ey,
            })
    return out[:5]


def page_setup(profile: dict, settings: dict, master: dict | None, job: dict) -> dict:
    """Everything autofill.js needs for one apply window (it asks for it once, through the page binding)."""
    opts = settings.get("autofill") or {}
    with_history = opts.get("experience", True) is not False
    return {
        "profile": profile,
        "rules": load_rules(),
        "history": {"experience": experience(master) if with_history else [],
                    "education": education(master) if with_history else []},
        "options": {"add_entries": bool(opts.get("add_entries")), "answers": opts.get("answers", True) is not False,
                    "capture": opts.get("capture", True) is not False},
        "disclosures": settings.get("disclosures") or {},
        "job": {"company": job.get("company") or "", "title": job.get("title") or ""},
    }
