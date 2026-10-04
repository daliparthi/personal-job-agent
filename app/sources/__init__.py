"""Job boards besides Workday: Greenhouse, Lever, Ashby and SmartRecruiters.

Each board publishes its open postings as public JSON. A board module has:
  NAME, LABEL       its id (stored in jobs.source and used in job ids) and display name
  API_HOST          the host its feed is served from
  FULL_BOARD        True when one request returns every open posting with its description; the board is then
                    filtered here with the same keyword, location and date rules Workday's search applies
  match(url)        (api_host, board) when the URL is one of this board's career pages, else None
  postings(client, site, query)  normalized postings (see below); query is used only by boards that search
  detail(client, site, ref)      the fields a listing left out (FULL_BOARD False only)

A normalized posting is a dict:
  ref, title, url, locations [str], countries [str] (empty when the board doesn't say), remote_raw,
  employment, time_type, posted (date or None), html (None until detail() fills it in), salary ((min, max) a year
  in USD, or None), salary_text, req_id.

Job ids are "<board>/<source>:<ref>", e.g. "airbnb/greenhouse:8184174"; Workday ids stay "<tenant>/<site>:<ref>".
"""
import re
from datetime import date

from ..jobparse import (CITY_STATE, NON_US_COUNTRIES, US_STATES, classify_employment, classify_remote,
                        contains_all, html_to_text, is_us_location, keywords_present, parse_salary, states_in)
from ..workday import Site
from ..workday import parse_site as parse_workday
from . import ashby, greenhouse, lever, smartrecruiters
from .base import locations

BOARDS = {m.NAME: m for m in (greenhouse, lever, ashby, smartrecruiters)}
LABELS = {"workday": "Workday", **{name: m.LABEL for name, m in BOARDS.items()}}
SUPPORTED = "Workday, Greenhouse, Lever, Ashby or SmartRecruiters"


def parse_site(name: str, url: str, ats: str | None = None, board: str | None = None) -> Site:
    """The career site for one companies.yaml entry. The source is detected from the URL; `ats:` (with `board:`)
    names it for a company whose careers page is on its own domain, e.g. ats: greenhouse, board: airbnb."""
    url = (url or "").strip()
    ats = (ats or "").strip().lower() or None
    board = (board or "").strip() or None
    if ats and ats not in LABELS:
        raise ValueError(f"Unknown ats: {ats} (use workday, greenhouse, lever, ashby or smartrecruiters)")
    if board and not ats:
        raise ValueError("board: needs ats: too (greenhouse, lever, ashby or smartrecruiters)")
    if ats == "workday":
        return parse_workday(name, url)
    for mod in BOARDS.values():
        if ats and mod.NAME != ats:
            continue
        hit = (mod.API_HOST, board) if board else mod.match(url)
        if hit:
            host, slug = hit
            return Site(name=name, url=url, host=host, tenant=slug, site=mod.NAME, source=mod.NAME)
    if ats:
        raise ValueError(f"Not a {LABELS[ats]} job board URL: {url} (or add board: <the board's name>)")
    try:
        return parse_workday(name, url)
    except ValueError:
        raise ValueError(f"Not a supported job board URL: {url} ({SUPPORTED})") from None


def source_of_key(company_key: str) -> str:
    """The source of a stored job from its company key (Workday keys are tenant/site)."""
    site = (company_key or "").rpartition("/")[2]
    return site if site in BOARDS else "workday"


# ---------------------------------------------------------------- filtering
_US_NAMES = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}
_ANYWHERE = re.compile(r"(fully |100% )?(remote|anywhere|worldwide|global|distributed)( first)?", re.I)
# Board locations are free text ("CA-Toronto", "IN - Bengaluru", "Remote - Texas", "NYC | US"), so a bare
# two-letter code is weak evidence; a country name, a state name, "City, ST" or a big US city is strong.
_EXPLICIT_US = re.compile(r"\b(?:us|usa|u\.s\.(?:a\.)?|united states(?: of america)?)\b", re.I)
_STATE_CODE = re.compile(r",\s*(?:" + "|".join(US_STATES) + r")\b")
_STATE_NAME = re.compile(r"\b(?:" + "|".join(re.escape(n.lower()) for n in US_STATES.values() if n != "Georgia") + r")\b")
_FOREIGN = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in sorted(NON_US_COUNTRIES, key=len, reverse=True)
                                         if len(c) >= 4 or c in ("uk", "uae")) + r")\b")


def _strong_us(loc: str) -> bool:
    low = loc.lower()
    return bool(_EXPLICIT_US.search(loc) or _STATE_CODE.search(loc) or _STATE_NAME.search(low)
                or any(part.strip() in CITY_STATE for part in re.split(r"[,\-–|/()]+", low)))


def _foreign(loc: str) -> bool:
    return bool(_FOREIGN.search(loc.lower().replace("new mexico", "")))


def us_location(loc: str) -> bool:
    return _strong_us(loc) or (not _foreign(loc) and is_us_location(loc))


def in_us(p) -> bool:
    """Is the posting open in the US? The board's country wins. Otherwise the location text decides: a clearly US
    location anywhere in the list, else no foreign country and a US state code, or just "Remote" (no country)."""
    countries = [c.strip().lower() for c in p.get("countries") or [] if c]
    if countries:
        return any(c in _US_NAMES for c in countries)
    locs = [loc for loc in p.get("locations") or [] if loc]
    if not locs:
        return True
    if any(_strong_us(loc) for loc in locs):
        return True
    if any(_foreign(loc) for loc in locs):
        return False
    return any(is_us_location(loc) for loc in locs) or any(_ANYWHERE.fullmatch(loc.strip()) for loc in locs)


def text_of(p) -> str:
    if p.get("text") is None:
        p["text"] = html_to_text(p.get("html") or "")
    return p["text"]


def select(postings, window: int, mandatory, optional, today=None):
    """The postings a Workday keyword search would have returned: posted within `window` days, open in the US,
    and (when the board sent the description) containing every mandatory keyword, or any optional one when there
    are no mandatory keywords. Boards that search (SmartRecruiters) are checked for keywords after detail()."""
    today = today or date.today()
    out = []
    for p in postings:
        if p.get("posted") and (today - p["posted"]).days > window:
            continue
        if not in_us(p):
            continue
        if p.get("html") is not None:
            hay = f"{p['title']}\n{text_of(p)}"
            if mandatory and not contains_all(hay, mandatory):
                continue
            if not mandatory and optional and not keywords_present(hay, optional):
                continue
        out.append(p)
    return out


# ---------------------------------------------------------------- the jobs-table row
def _readable(kind: str) -> str:
    """"FullTime" -> "Full-Time"; other values unchanged."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", "-", kind or "")


def to_job(site: Site, company: str, p: dict, today=None) -> dict:
    """A jobs-table row (the same fields SearchRunner._detail stores for Workday) from a normalized posting."""
    today = today or date.today()
    html = p.get("html") or ""
    text = text_of(p)
    title = (p.get("title") or "").strip()
    locs = locations(*(p.get("locations") or []))
    us_locs = [loc for loc in locs if us_location(loc)]
    states = sorted(set().union(*(states_in(loc) for loc in us_locs))) if us_locs else []
    if p.get("salary"):
        smin, smax = p["salary"]
        stext = p.get("salary_text") or f"${smin:,.0f} - ${smax:,.0f}"
    else:
        smin, smax, stext = parse_salary(text)
    employment = _readable(p.get("employment") or "")
    posted = p.get("posted") or today
    return {
        "id": f"{site.key}:{p['ref']}", "company": company, "company_key": site.key, "tenant": site.tenant,
        "site": site.site, "source": site.source, "title": title, "url": p.get("url") or site.url,
        "external_path": str(p["ref"]), "req_id": str(p.get("req_id") or p["ref"]),
        "location": (us_locs or locs or [""])[0], "locations_json": us_locs + [loc for loc in locs if loc not in us_locs],
        "states_json": states, "country": "United States", "remote_raw": p.get("remote_raw") or "",
        "remote_type": classify_remote(p.get("remote_raw"), locs, title, text),
        "employment_type": classify_employment(employment, p.get("time_type") or employment, title, text),
        "worker_sub_type": employment, "time_type": p.get("time_type") or "",
        "salary_min": smin, "salary_max": smax, "salary_text": stext,
        "posted_date": min(posted, today).isoformat(), "description_html": html, "description_text": text,
    }

