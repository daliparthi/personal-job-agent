"""Ashby job boards: jobs.ashbyhq.com/<board>.

GET api.ashbyhq.com/posting-api/job-board/<board>?includeCompensation=true returns every listed posting with its
description, address (with country) and, when the company publishes it, the pay range."""
import re

from .base import locations, per_year, when

NAME, LABEL = "ashby", "Ashby"
API_HOST = "api.ashbyhq.com"
FULL_BOARD = True

_URL = re.compile(r"https?://(?:jobs\.ashbyhq\.com/|api\.ashbyhq\.com/posting-api/job-board/)(?P<board>[\w.%-]+)", re.I)


def match(url: str):
    m = _URL.match(url or "")
    return (API_HOST, m.group("board")) if m else None


async def postings(client, site, query=""):
    data = await client.get_json(f"https://{site.host}/posting-api/job-board/{site.tenant}",
                                 {"includeCompensation": "true"})
    return [posting(j) for j in data.get("jobs") or [] if j.get("isListed", True) is not False]


def _country(place: dict) -> str:
    return ((place.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""


def _salary(comp: dict):
    for c in comp.get("summaryComponents") or []:
        if c.get("compensationType") == "Salary" and (c.get("currencyCode") or "USD").upper() == "USD":
            return per_year(c.get("minValue"), c.get("maxValue"), c.get("interval"))
    return None


def posting(j: dict) -> dict:
    places = [j, *(j.get("secondaryLocations") or [])]
    countries = [_country(p) for p in places]
    workplace = j.get("workplaceType") or ("Remote" if j.get("isRemote") else "")
    comp = j.get("compensation") or {}
    salary = _salary(comp)
    return {
        "ref": str(j["id"]), "title": (j.get("title") or "").strip(), "url": j.get("jobUrl"),
        "locations": locations(*(p.get("location") for p in places)),
        "countries": countries if all(countries) else [],  # one location without a country: let the text decide
        "remote_raw": workplace, "employment": j.get("employmentType") or "", "time_type": j.get("employmentType") or "",
        "posted": when(j.get("publishedAt")), "html": j.get("descriptionHtml") or "", "salary": salary,
        "salary_text": (comp.get("scrapeableCompensationSalarySummary") or "") if salary else "", "req_id": "",
    }
