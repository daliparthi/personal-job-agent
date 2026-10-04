"""Greenhouse job boards: boards.greenhouse.io/<board> or job-boards.greenhouse.io/<board>.

GET boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true returns every open posting with its description
(HTML-escaped HTML). Greenhouse has no separate country field, so the location text decides."""
import html
import re

from .base import locations, when

NAME, LABEL = "greenhouse", "Greenhouse"
API_HOST = "boards-api.greenhouse.io"
FULL_BOARD = True

_URLS = (
    re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?(?P<board>[\w-]+)", re.I),
    re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/(?P<board>[\w-]+)", re.I),
)


def match(url: str):
    for rx in _URLS:
        m = rx.match(url or "")
        if m and m.group("board").lower() not in ("embed", "v1"):
            return API_HOST, m.group("board")
    return None


async def postings(client, site, query=""):
    data = await client.get_json(f"https://{site.host}/v1/boards/{site.tenant}/jobs", {"content": "true"})
    return [posting(j) for j in data.get("jobs") or []]


def _value(v) -> str:
    if isinstance(v, list):
        return ", ".join(str(x) for x in v if x not in (None, ""))
    return "" if v is None or isinstance(v, bool) else str(v)


def posting(j: dict) -> dict:
    meta = {(m.get("name") or "").strip().lower(): _value(m.get("value")) for m in j.get("metadata") or []}
    offices = [o.get("location") or o.get("name") for o in j.get("offices") or []]
    return {
        "ref": str(j["id"]), "title": (j.get("title") or "").strip(), "url": j.get("absolute_url"),
        "locations": locations((j.get("location") or {}).get("name"), *offices), "countries": [],
        "remote_raw": meta.get("workplace type") or meta.get("location type") or meta.get("remote") or "",
        "employment": meta.get("employment type") or meta.get("job type") or "", "time_type": "",
        "posted": when(j.get("first_published") or j.get("updated_at")),
        "html": html.unescape(j.get("content") or ""), "salary": None, "salary_text": "",
        "req_id": j.get("requisition_id") or "",
    }
