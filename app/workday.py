"""Minimal client for the public Workday career-site JSON API (the same calls the career page makes)."""
import asyncio
import re
from dataclasses import dataclass

import httpx

_URL_RE = re.compile(
    r"https?://(?P<host>(?P<tenant>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)")
_SITE_RE = re.compile(
    r"https?://(?P<host>wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/(?P<tenant>[\w-]+)/(?P<site>[^/?#]+)")

_HOST_RE = re.compile(r"(?:^|\.)(?:myworkdayjobs|myworkdaysite)\.com$", re.I)


def is_workday_host(host) -> bool:
    """True for a Workday career-site host name (exact suffix match, never a substring of the URL)."""
    return bool(host and _HOST_RE.search(host))


HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "en-US",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/141.0 Safari/537.36",
}


@dataclass
class Site:
    name: str
    url: str
    host: str
    tenant: str
    site: str

    @property
    def key(self):
        return f"{self.tenant}/{self.site}".lower()

    @property
    def api(self):
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"


def parse_site(name: str, url: str) -> Site:
    url = (url or "").strip()
    m = _URL_RE.match(url) or _SITE_RE.match(url)
    if not m:
        raise ValueError(f"Not a Workday career-site URL: {url}")
    return Site(name=name, url=url, host=m.group("host"), tenant=m.group("tenant"), site=m.group("site"))


class WorkdayClient:
    def __init__(self):
        self.http = httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(30.0), follow_redirects=True,
                                      limits=httpx.Limits(max_connections=20))

    async def close(self):
        await self.http.aclose()

    async def _request(self, method, url, **kw):
        delay = 2.0
        for attempt in range(4):
            try:
                r = await self.http.request(method, url, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt == 3 or (status and status not in (429, 500, 502, 503, 504)):
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    async def list_jobs(self, site: Site, search_text="", facets=None, offset=0, limit=20):
        body = {"appliedFacets": facets or {}, "limit": limit, "offset": offset, "searchText": search_text}
        return await self._request("POST", f"{site.api}/jobs", json=body)

    async def job_detail(self, site: Site, external_path: str):
        return await self._request("GET", f"{site.api}{external_path}")


# ---------------------------------------------------------------- facet helpers
def _walk(facets):
    for f in facets or []:
        values = f.get("values", [])
        nested = [v for v in values if "facetParameter" in v]
        if nested:
            yield from _walk(nested)
        else:
            yield f


_US_NAMES = {"united states of america", "united states", "usa", "us", "u.s.", "u.s.a."}


def find_us_facet(facets):
    """(facetParameter, id) for the 'United States' country value, if the site exposes one."""
    for f in _walk(facets):
        for v in f.get("values", []):
            if v.get("descriptor", "").strip().lower() in _US_NAMES:
                return f["facetParameter"], v["id"]
    return None


def find_job_type_facet(facets):
    """(facetParameter, [(id, descriptor, count)]) for Workday's worker sub-type ('Job Type') facet."""
    for f in _walk(facets):
        param = f.get("facetParameter", "")
        label = (f.get("descriptor") or "").lower()
        if param == "workerSubType" or label in ("job type", "worker sub-type", "worker sub type",
                                                 "employee type", "employment type"):
            vals = [(v["id"], v.get("descriptor", ""), v.get("count", 0)) for v in f.get("values", [])]
            if vals:
                return param, vals
    return None
