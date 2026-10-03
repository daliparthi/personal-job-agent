import asyncio

import httpx
import pytest

from app import workday
from app.workday import WorkdayClient, find_job_type_facet, find_us_facet, is_workday_host, parse_site


@pytest.mark.parametrize("url, host, tenant, site", [
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
     "nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite"),
    ("https://acme.wd1.myworkdayjobs.com/en-US/External?q=python#top", "acme.wd1.myworkdayjobs.com", "acme", "External"),
    ("https://wd3.myworkdaysite.com/recruiting/globex/Careers", "wd3.myworkdaysite.com", "globex", "Careers"),
    ("https://wd3.myworkdaysite.com/en-US/recruiting/globex/Careers/", "wd3.myworkdaysite.com", "globex", "Careers"),
])
def test_parse_site(url, host, tenant, site):
    s = parse_site("Name", f"  {url}  ")
    assert (s.host, s.tenant, s.site) == (host, tenant, site)
    assert s.key == f"{tenant}/{site}".lower()
    assert s.api == f"https://{host}/wday/cxs/{tenant}/{site}"


@pytest.mark.parametrize("url", ["https://jobs.lever.co/acme", "", None, "nvidia.wd5.myworkdayjobs.com/X"])
def test_parse_site_rejects_other_urls(url):
    with pytest.raises(ValueError):
        parse_site("Name", url)


@pytest.mark.parametrize("host, want", [
    ("nvidia.wd5.myworkdayjobs.com", True), ("wd3.myworkdaysite.com", True), ("NVIDIA.WD5.MYWORKDAYJOBS.COM", True),
    ("myworkdayjobs.com.evil.com", False), ("evilmyworkdayjobs.com", False), ("example.com", False), (None, False),
])
def test_is_workday_host(host, want):
    assert is_workday_host(host) is want


NESTED_FACETS = [
    {"facetParameter": "locationMainGroup", "descriptor": "Locations", "values": [
        {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
            {"descriptor": "Canada", "id": "ca1", "count": 3},
            {"descriptor": "United States of America", "id": "us1", "count": 9},
        ]},
    ]},
    {"facetParameter": "workerSubType", "descriptor": "Job Type", "values": [
        {"id": "reg", "descriptor": "Regular", "count": 8}, {"id": "con", "descriptor": "Contractor", "count": 1},
    ]},
]


def test_find_facets_walks_nested_groups():
    assert find_us_facet(NESTED_FACETS) == ("locationCountry", "us1")
    assert find_job_type_facet(NESTED_FACETS) == ("workerSubType", [("reg", "Regular", 8), ("con", "Contractor", 1)])
    assert find_us_facet([]) is None and find_job_type_facet(None) is None


def test_job_type_facet_found_by_label():
    facets = [{"facetParameter": "abc123", "descriptor": "Employee Type", "values": [{"id": "x", "descriptor": "Full"}]}]
    assert find_job_type_facet(facets) == ("abc123", [("x", "Full", 0)])


def _client(handler):
    return WorkdayClient(transport=httpx.MockTransport(handler))


def test_request_retries_retryable_status(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(workday.asyncio, "sleep", no_sleep)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"total": 0})

    async def run():
        c = _client(handler)
        try:
            return await c.list_jobs(parse_site("A", "https://a.wd1.myworkdayjobs.com/S"), "python")
        finally:
            await c.close()

    assert asyncio.run(run()) == {"total": 0}
    assert len(calls) == 3
    body = calls[0].read()
    assert b'"searchText":"python"' in body.replace(b" ", b"")


def test_request_does_not_retry_client_errors(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    async def run():
        c = _client(handler)
        try:
            await c.job_detail(parse_site("A", "https://a.wd1.myworkdayjobs.com/S"), "/job/x")
        finally:
            await c.close()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert len(calls) == 1
    assert str(calls[0].url) == "https://a.wd1.myworkdayjobs.com/wday/cxs/a/S/job/x"
