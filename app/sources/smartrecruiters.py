"""SmartRecruiters job boards: jobs.smartrecruiters.com/<company> or careers.smartrecruiters.com/<company>.

The listing (api.smartrecruiters.com/v1/companies/<company>/postings) is searched with the same keywords as
Workday and limited to US postings; each posting's description takes one more request (detail)."""
import re

from ..config import MAX_PAGES_PER_QUERY
from .base import locations, when

NAME, LABEL = "smartrecruiters", "SmartRecruiters"
API_HOST = "api.smartrecruiters.com"
FULL_BOARD = False

_URL = re.compile(r"https?://(?:(?:jobs|careers)\.smartrecruiters\.com/|api\.smartrecruiters\.com/v1/companies/)"
                  r"(?P<board>[\w-]+)", re.I)
PAGE = 100
MAX_POSTINGS = MAX_PAGES_PER_QUERY * 20  # the same cap as one Workday search


def match(url: str):
    m = _URL.match(url or "")
    return (API_HOST, m.group("board")) if m else None


async def postings(client, site, query=""):
    out, offset = [], 0
    while offset < MAX_POSTINGS:
        params = {"country": "us", "limit": PAGE, "offset": offset}
        if query:
            params["q"] = query
        data = await client.get_json(f"https://{site.host}/v1/companies/{site.tenant}/postings", params)
        content = data.get("content") or []
        out += [posting(site, p) for p in content]
        offset += len(content)
        if not content or offset >= (data.get("totalFound") or 0):
            break
    return out


def posting(site, p: dict) -> dict:
    loc = p.get("location") or {}
    workplace = "Remote" if loc.get("remote") else "Hybrid" if loc.get("hybrid") else ""
    kind = (p.get("typeOfEmployment") or {}).get("label") or ""
    return {
        "ref": str(p["id"]), "title": (p.get("name") or "").strip(),
        "url": f"https://jobs.smartrecruiters.com/{site.tenant}/{p['id']}",
        "locations": locations(loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)),
        "countries": [loc["country"]] if loc.get("country") else [],
        "remote_raw": workplace, "employment": kind, "time_type": kind, "posted": when(p.get("releasedDate")),
        "html": None, "salary": None, "salary_text": "", "req_id": p.get("refNumber") or "",
    }


async def detail(client, site, ref: str) -> dict:
    """The description and the posting's page. active is False once the posting is closed."""
    d = await client.get_json(f"https://{site.host}/v1/companies/{site.tenant}/postings/{ref}")
    sections = ((d.get("jobAd") or {}).get("sections") or {})
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key) or {}
        if sec.get("text"):
            parts.append(f"<h3>{sec.get('title') or ''}</h3>{sec['text']}")
    return {"html": "\n".join(parts), "url": d.get("postingUrl") or None, "active": d.get("active", True) is not False}
