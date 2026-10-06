"""Find Workday career sites from a Google search, so a keyword search is not limited to the companies in companies.yaml.

For mandatory keywords such as sql the query is   site:myworkdayjobs.com "sql"   and every Workday URL in the results
(any company's job page or career-site page) becomes a career site to search. If Google can't be reached or blocks
the request, find_workday_sites() raises DiscoveryError and the caller falls back to the Workday entries in
companies.yaml.

Two ways to ask Google:
  * Google's Programmable Search JSON API, when GOOGLE_API_KEY and GOOGLE_CSE_ID are set in the personal .env file.
    Reliable. The search engine only needs to cover *.myworkdayjobs.com.
  * Otherwise the public results page. In practice Google answers a plain HTTP client with a JavaScript-only,
    consent or "unusual traffic" page; that is reported as DiscoveryError, never worked around.
"""
import asyncio
import html
import re
from urllib.parse import unquote

import httpx

from . import envfile
from .workday import HEADERS as _WORKDAY_HEADERS
from .workday import Site, parse_site

DOMAINS = ("myworkdayjobs.com", "myworkdaysite.com")
MAX_PAGES = 5        # result pages per search (10 results each)
PAGE_DELAY = 2.0     # seconds between result pages, to stay polite
RECENCY = "w"        # Google's "past week" filter (d = day, w = week, m = month, "" = any time)

_BROWSER_HEADERS = {
    "User-Agent": _WORKDAY_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
_WORKDAY_URL = re.compile(
    r"https?://(?:[\w-]+\.wd\d+\.myworkdayjobs\.com|wd\d+\.myworkdaysite\.com)/[^\s\"'<>&\\)]*", re.I)
_NOT_A_SITE = {"job", "details", "wday", "login"}  # path segments that follow the host when the site name is missing


class DiscoveryError(Exception):
    """Google could not be searched (blocked, rate limited, network error or no API access)."""


def build_query(keywords) -> str:
    """site:myworkdayjobs.com "sql" "python" - every mandatory keyword, quoted."""
    terms = " ".join('"' + k.replace('"', "") + '"' for k in keywords if k.strip())
    return f"site:myworkdayjobs.com {terms}".strip()


def sites_from_text(text: str) -> list[Site]:
    """Every distinct Workday career site (tenant/site) that has a URL in `text`, in first-seen order."""
    found = {}
    for url in _WORKDAY_URL.findall(unquote(html.unescape(text or ""))):
        try:
            site = parse_site("", url.rstrip(".,;"))
        except ValueError:
            continue
        if site.site.lower() in _NOT_A_SITE:
            continue
        found.setdefault(site.key, site)
    return list(found.values())


def _with_name(site: Site) -> Site:
    # The company name isn't in a Workday URL; the tenant (e.g. "nvidia") is the best label there is.
    site.name = site.tenant.replace("-", " ").replace("_", " ").title()
    site.url = f"https://{site.host}/{site.site}"
    return site


async def _google_page(http, query, page) -> str:
    params = {"q": query, "hl": "en", "start": page * 10}
    if RECENCY:
        params["tbs"] = f"qdr:{RECENCY}"
    r = await http.get("https://www.google.com/search", params=params)
    if r.status_code == 429 or "/sorry/" in str(r.url) or "unusual traffic" in r.text[:20000]:
        raise DiscoveryError("Google blocked the search (it asks for a CAPTCHA). Try again later or set "
                             "GOOGLE_API_KEY and GOOGLE_CSE_ID in .env to use Google's search API.")
    if "consent.google.com" in str(r.url):
        raise DiscoveryError("Google showed a cookie-consent page instead of results.")
    r.raise_for_status()
    if "not redirected within a few seconds" in r.text or "enablejs" in r.text:
        # Google sends a JavaScript-only page (no results) to clients that are not a real browser.
        raise DiscoveryError("Google only returns results to a real browser. Set GOOGLE_API_KEY and GOOGLE_CSE_ID "
                             "in .env to use Google's search API (see README, 'Finding Workday sites with Google').")
    return r.text


async def _api_page(http, query, page, key, cx) -> str:
    params = {"key": key, "cx": cx, "q": query, "start": page * 10 + 1, "num": 10}
    if RECENCY:
        params["dateRestrict"] = f"{RECENCY}1"
    r = await http.get("https://www.googleapis.com/customsearch/v1", params=params)
    if r.status_code in (403, 429):
        raise DiscoveryError(f"Google search API refused the request ({r.status_code}): quota used up or key invalid.")
    r.raise_for_status()
    return " ".join(i.get("link", "") for i in r.json().get("items", []))


async def find_workday_sites(keywords, max_pages=MAX_PAGES, delay=PAGE_DELAY, transport=None, log=None) -> list[Site]:
    """Workday career sites that appear in Google results for site:myworkdayjobs.com "<keywords>".
    Raises DiscoveryError when nothing could be read from Google at all; a later page failing keeps what was found."""
    if not any(k.strip() for k in keywords):
        return []
    query = build_query(keywords)
    env = envfile.read()
    key, cx = env.get("GOOGLE_API_KEY", ""), env.get("GOOGLE_CSE_ID", "")
    found: dict[str, Site] = {}
    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=httpx.Timeout(20.0), follow_redirects=True,
                                 cookies={"CONSENT": "YES+"}, transport=transport) as http:
        for page in range(max_pages):
            try:
                text = await (_api_page(http, query, page, key, cx) if key and cx else _google_page(http, query, page))
            except (DiscoveryError, httpx.HTTPError) as e:
                if not found:
                    raise DiscoveryError(str(e) if isinstance(e, DiscoveryError) else f"Google search failed: {e!r}")
                if log:
                    log(f"Google search stopped at page {page + 1}: {e}")
                break
            new = [s for s in sites_from_text(text) if s.key not in found]
            for s in new:
                found[s.key] = _with_name(s)
            if not new and page:  # a page with nothing new: the results have run out
                break
            if page + 1 < max_pages:
                await asyncio.sleep(delay)
    return list(found.values())
