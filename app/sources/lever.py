"""Lever job boards: jobs.lever.co/<board> (or jobs.eu.lever.co/<board> for the EU instance).

GET api.lever.co/v0/postings/<board>?mode=json returns every open posting: the description, the bulleted lists
(requirements, responsibilities) and the closing text are separate fields, joined here into one description."""
import re

from .base import locations, per_year, when

NAME, LABEL = "lever", "Lever"
API_HOST = "api.lever.co"
FULL_BOARD = True

_URL = re.compile(r"https?://(?:jobs|api)\.(?P<eu>eu\.)?lever\.co/(?:v0/postings/)?(?P<board>[\w.-]+)", re.I)


def match(url: str):
    m = _URL.match(url or "")
    if not m or m.group("board").lower() == "v0":
        return None
    return f"api.{m.group('eu') or ''}lever.co", m.group("board")


async def postings(client, site, query=""):
    data = await client.get_json(f"https://{site.host}/v0/postings/{site.tenant}", {"mode": "json"})
    return [posting(j) for j in data or [] if isinstance(j, dict)]


def _html(j: dict) -> str:
    parts = [j.get("description") or j.get("opening") or ""]
    for lst in j.get("lists") or []:
        parts.append(f"<h3>{lst.get('text') or ''}</h3><ul>{lst.get('content') or ''}</ul>")
    parts.append(j.get("additional") or "")
    return "\n".join(p for p in parts if p)


def posting(j: dict) -> dict:
    cat = j.get("categories") or {}
    pay = j.get("salaryRange") or {}
    salary = None
    if pay and (pay.get("currency") or "USD").upper() == "USD":
        salary = per_year(pay.get("min"), pay.get("max"), pay.get("interval"))
    workplace = j.get("workplaceType") or ""
    return {
        "ref": str(j["id"]), "title": (j.get("text") or "").strip(), "url": j.get("hostedUrl"),
        "locations": locations(cat.get("location"), *(cat.get("allLocations") or [])),
        "countries": [j["country"]] if j.get("country") else [],
        "remote_raw": "" if workplace == "unspecified" else workplace,
        "employment": cat.get("commitment") or "", "time_type": cat.get("commitment") or "",
        "posted": when(j.get("createdAt")), "html": _html(j), "salary": salary, "salary_text": "",
        "req_id": "",
    }
