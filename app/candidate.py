"""What the match score knows about you, from master_resume.yaml and your Settings profile.

    years_total      years of experience, from the start/end dates of your jobs (overlapping jobs counted once)
    years_by_skill   for each skill in the keyword dictionary: the years of the jobs whose lines mention it
    recent_title     the title of your latest job, and its seniority level (see jobparse.seniority_level)
    degree           your highest degree (1 bachelor, 2 master, 3 PhD) or None when there is no education section
    bullets          your resume lines, used as evidence for a posting's requirements
    needs_sponsorship / us_citizen / has_clearance   from Settings > Applicant profile, for the knockout checks
"""
import json
import re
from datetime import date

from .jobparse import degree_level, seniority_level
from .lexicon import ENTRIES
from .scoring import has_any

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), start=1)}
_SEASONS = {"winter": 1, "spring": 4, "summer": 7, "fall": 10, "autumn": 10}


def parse_when(text, end=False, today=None):
    """(year, month) of a resume date: "Jan 2020", "March, 2021", "03/2019", "Summer 2015", "2018", "Present".
    "Present" means `today` (default: the real today)."""
    s = (text or "").strip().lower()
    if not s:
        return None
    today = today or date.today()
    if re.match(r"(present|current|now|today|ongoing)", s):
        return today.year, today.month
    m = re.search(r"\b(\d{1,2})/((?:19|20)\d{2})\b", s)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"\b([a-z]{3,9})\.?,?\s+((?:19|20)\d{2})\b", s)
    if m:
        word = m.group(1)
        month = _MONTHS.get(word[:3]) if word[:3] in _MONTHS else _SEASONS.get(word)
        if month:
            return int(m.group(2)), month
    m = re.search(r"\b((?:19|20)\d{2})\b", s)
    if m:
        return int(m.group(1)), 12 if end else 1
    return None


def _month_index(ym):
    return ym[0] * 12 + ym[1] - 1


def _merged_months(spans) -> int:
    """Months covered by [(start_index, end_index)] spans, overlaps counted once."""
    total, cur = 0, None
    for a, b in sorted(spans):
        if cur and a <= cur[1] + 1:
            cur = (cur[0], max(cur[1], b))
        else:
            if cur:
                total += cur[1] - cur[0] + 1
            cur = (a, b)
    if cur:
        total += cur[1] - cur[0] + 1
    return total


def _entry_text(e) -> str:
    return "\n".join([e.get("title") or "", e.get("company") or "", e.get("description") or "",
                      *(e.get("heading") or []), *(e.get("bullets") or [])])


def _entries(master, kind):
    return [e for s in (master or {}).get("sections") or [] if s.get("kind") == kind
            for e in s.get("entries") or [] if isinstance(e, dict)]


_cache = {}


def profile(master: dict | None, applicant: dict | None = None, as_of: date | None = None) -> dict:
    """The candidate profile for scoring (cached: the same resume and settings give the same profile).
    as_of: what "Present" in the resume's dates means (default: today)."""
    as_of = as_of or date.today()
    key = json.dumps([master, {k: (applicant or {}).get(k) for k in ("needs_sponsorship", "us_citizen", "has_clearance")},
                      as_of.isoformat()], sort_keys=True, default=str)
    if key not in _cache:
        if len(_cache) > 8:
            _cache.clear()
        _cache[key] = _build(master or {}, applicant or {}, as_of)
    return _cache[key]


def profile_for(resume: dict | None, settings: dict) -> dict | None:
    """The scoring profile of the stored master resume (db.get_resume()), or None without one.

    "Present" counts up to the day the resume was last saved, not to today. Otherwise the years of experience would
    grow with the calendar and the same resume would score the same posting differently from one month to the next;
    saving the resume again brings the dates up to date."""
    if not resume:
        return None
    try:
        as_of = date.fromisoformat((resume.get("uploaded_at") or "")[:10])
    except ValueError:
        as_of = None
    return profile(resume["data"], settings["profile"], as_of)


def _build(master, applicant, as_of=None) -> dict:
    jobs = []
    for e in _entries(master, "experience"):
        start, end = parse_when(e.get("start"), today=as_of), parse_when(e.get("end"), end=True, today=as_of)
        if start and end and end >= start:
            jobs.append((_month_index(start), _month_index(end), e))
    years_total = round(_merged_months([(a, b) for a, b, _ in jobs]) / 12, 1) if jobs else None
    by_skill = {}
    for canon, aliases, _ in ENTRIES:
        spans = [(a, b) for a, b, e in jobs if has_any(aliases, _entry_text(e))]
        if spans:
            by_skill[canon] = round(_merged_months(spans) / 12, 1)
    if jobs:
        recent = max(jobs, key=lambda j: (j[1], j[0]))[2]
    else:
        recent = next(iter(_entries(master, "experience")), {})
    recent_title = recent.get("title") or (recent.get("heading") or [""])[0]
    degrees = [degree_level(" ".join([e.get("degree") or "", *(e.get("heading") or [])]))
               for e in _entries(master, "education")]
    degrees = [d for d in degrees if d]
    bullets = []
    for s in master.get("sections") or []:
        if s.get("kind") in ("experience", "projects"):
            for e in s.get("entries") or []:
                bullets += [b for b in e.get("bullets") or [] if b]
                if e.get("description"):
                    bullets.append(e["description"])
        elif s.get("kind") == "summary":
            bullets += [x for x in [s.get("text"), *(s.get("bullets") or [])] if x]
    return {"years_total": years_total, "years_by_skill": by_skill, "recent_title": recent_title,
            "level": seniority_level(recent_title), "degree": max(degrees) if degrees else None,
            "has_education": bool(_entries(master, "education")), "bullets": bullets,
            "needs_sponsorship": applicant.get("needs_sponsorship") == "Yes",
            "us_citizen": applicant.get("us_citizen") or "", "has_clearance": applicant.get("has_clearance") or ""}
