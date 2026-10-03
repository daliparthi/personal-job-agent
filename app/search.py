"""Search orchestration: incremental/full pulls across the company lists, plus the filtered/sorted job view."""
import asyncio
import hashlib
import re
from datetime import date, datetime, timedelta

import yaml

from . import db, scoring
from .config import (COMPANIES_SHARED, COMPANY_CONCURRENCY, DETAIL_CONCURRENCY, MAX_PAGES_PER_QUERY,
                     MY_COMPANIES, OLD_COMPANIES_COPY, RETENTION_DAYS)
from .jobparse import (classify_employment, classify_remote, contains_all, html_to_text, is_us_location,
                       keywords_present, looks_non_us, parse_salary, posted_days_ago, split_keywords, states_in)
from .workday import WorkdayClient, find_job_type_facet, find_us_facet, parse_site


# ---------------------------------------------------------------- company lists
# companies.yaml in the program folder is the shared list; my_companies.yaml in your personal folder holds your own
# additions (Settings > Add company) and overrides (same URL, e.g. enabled: false). Both are read fresh for every
# search and every time Settings opens, so edits apply without restarting.
MY_HEADER = """\
# my_companies.yaml - your own Workday sites, added to the shared companies.yaml in the program folder.
# Same format. An entry with the same URL as a shared one replaces it for you (e.g. add  enabled: false).
companies:
"""


def _read_list(path, source):
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" line {mark.line + 1}" if mark else ""
        return [{"name": path.name, "url": "", "key": None, "error": f"YAML error on{where}: {getattr(e, 'problem', e)}",
                 "enabled": True, "aliases": [], "source": source}]
    out = []
    for c in (raw.get("companies") if isinstance(raw, dict) else None) or []:
        if not isinstance(c, dict):
            continue
        name, url = str(c.get("name", "")).strip(), str(c.get("url", "")).strip()
        try:
            key, err = parse_site(name, url).key, None
        except ValueError as e:
            key, err = None, str(e)
        aliases = c.get("aliases") or []
        aliases = [str(a).strip() for a in (aliases if isinstance(aliases, list) else [aliases]) if str(a).strip()]
        out.append({"name": name, "url": url, "key": key, "error": err, "enabled": c.get("enabled", True) is not False,
                    "aliases": aliases, "source": source})
    return out


def _migrate_old_copy():
    """Earlier versions copied the shared list into the personal folder on first run, so later edits to the shared
    companies.yaml were ignored. Keep only entries that aren't in the shared list (your additions), set the copy aside."""
    if not OLD_COMPANIES_COPY.exists():
        return
    shared = {c["key"] for c in _read_list(COMPANIES_SHARED, "shared") if c["key"]}
    mine = {c["key"] for c in _read_list(MY_COMPANIES, "yours") if c["key"]}
    for c in _read_list(OLD_COMPANIES_COPY, "yours"):
        if c["key"] and c["key"] not in shared and c["key"] not in mine:
            append_company(c["name"], c["url"], enabled=c["enabled"], aliases=c["aliases"])
    OLD_COMPANIES_COPY.replace(OLD_COMPANIES_COPY.with_name("companies.yaml.old"))


def load_companies():
    _migrate_old_copy()
    out, index = [], {}
    for c in _read_list(COMPANIES_SHARED, "shared") + _read_list(MY_COMPANIES, "yours"):
        k = c["key"] or f"{c['source']}:{c['url'] or c['name']}"
        if k in index:
            out[index[k]] = c  # your entry replaces the shared one, in the same place
        else:
            index[k] = len(out)
            out.append(c)
    return out


def append_company(name: str, url: str, enabled=True, aliases=()):
    parse_site(name, url)  # validates
    text = MY_COMPANIES.read_text(encoding="utf-8") if MY_COMPANIES.exists() else MY_HEADER
    if not text.endswith("\n"):
        text += "\n"
    safe = name.replace('"', "'")
    text += f'  - name: "{safe}"\n    url: {url.strip()}\n'
    if not enabled:
        text += "    enabled: false\n"
    if aliases:
        text += "    aliases: [" + ", ".join('"' + a.replace('"', "'") + '"' for a in aliases) + "]\n"
    MY_COMPANIES.write_text(text, encoding="utf-8")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_current_employer(company_name: str, site_key: str, employer: str, aliases=()) -> bool:
    """True when `employer` (Settings > Current employer) names this company, its Workday tenant or an alias."""
    e = _norm(employer)
    if len(e) < 3:
        return False
    tenant = _norm((site_key or "").split("/")[0])
    for n in (company_name, *aliases):
        c = _norm(n)
        if e in c or (len(c) >= 3 and c in e):
            return True
    return e == tenant


def keyword_signature(mandatory, optional):
    basis = sorted(k.lower() for k in mandatory) or ["opt:" + k.lower() for k in sorted(optional)]
    return hashlib.sha1("|".join(basis).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- runner
class SearchRunner:
    def __init__(self):
        self.task = None
        self.stop_requested = False
        self.state = self._blank()

    @staticmethod
    def _blank():
        return {"running": False, "mode": None, "started_at": None, "finished_at": None, "total": 0, "done": 0,
                "current": [], "scanned": 0, "new_jobs": 0, "refreshed": 0, "rejected": 0, "log": [], "errors": []}

    def log(self, msg):
        self.state["log"].append(f"{datetime.now():%H:%M:%S}  {msg}")
        self.state["log"] = self.state["log"][-300:]

    def start(self, full_refresh: bool):
        if self.state["running"]:
            raise RuntimeError("A search is already running")
        self.stop_requested = False
        self.state = self._blank()
        self.state.update(running=True, mode="full refresh" if full_refresh else "incremental",
                          started_at=datetime.now().isoformat(timespec="seconds"))
        try:
            self.task = asyncio.get_running_loop().create_task(self._run(full_refresh))
        except RuntimeError:
            self.state["running"] = False
            raise

    def stop(self):
        self.stop_requested = True
        self.log("Stop requested — finishing in-flight requests…")

    async def _run(self, full_refresh):
        client = WorkdayClient()
        try:
            settings = db.get_settings()
            mandatory = split_keywords(settings["mandatory"])
            optional = split_keywords(settings["optional"])
            if not mandatory and not optional:
                self.log("Enter at least one mandatory or optional keyword before searching.")
                return
            purged = db.purge_old()
            if purged:
                self.log(f"Purged {purged} untouched postings older than {RETENTION_DAYS} days")
            resume = db.get_resume()
            resume_text = resume["text"] if resume else ""
            sig = keyword_signature(mandatory, optional)
            disabled = set(settings.get("disabled_companies") or [])
            companies = []
            for c in load_companies():
                if c["error"]:
                    self.state["errors"].append(f"{c['name']}: {c['error']}")
                    continue
                if not c["enabled"] or c["key"] in disabled:
                    continue
                if is_current_employer(c["name"], c["key"], settings.get("current_employer"), c["aliases"]):
                    self.log(f"Skipping {c['name']} (your current employer)")
                    continue
                companies.append(c)
            self.state["total"] = len(companies)
            queries = [" ".join(mandatory)] if mandatory else optional
            self.log(f"{self.state['mode'].title()} run over {len(companies)} companies; "
                     f"Workday search: {' | '.join(repr(q) for q in queries)}")
            sem = asyncio.Semaphore(COMPANY_CONCURRENCY)
            await asyncio.gather(*(self._company(client, sem, c, queries, mandatory, optional, sig, full_refresh,
                                                 resume_text) for c in companies))
            self.log(f"Done. {self.state['new_jobs']} new, {self.state['refreshed']} refreshed, "
                     f"{self.state['rejected']} dropped by keyword/location checks.")
        except Exception as e:  # surface anything unexpected in the UI
            self.state["errors"].append(f"Search failed: {e!r}")
            self.log(f"Search failed: {e!r}")
        finally:
            await client.close()
            self.state["running"] = False
            self.state["current"] = []
            self.state["finished_at"] = datetime.now().isoformat(timespec="seconds")

    async def _company(self, client, sem, comp, queries, mandatory, optional, sig, full_refresh, resume_text):
        async with sem:
            if self.stop_requested:
                return
            name = comp["name"]
            self.state["current"].append(name)
            started = datetime.now()
            try:
                site = parse_site(name, comp["url"])
                last = None if full_refresh else db.last_success(site.key, sig)
                if last:
                    window = min(RETENTION_DAYS, (date.today() - last.date()).days + 1)
                    self.log(f"{name}: incremental — postings from the last {window} day(s)")
                else:
                    window = RETENTION_DAYS
                    self.log(f"{name}: pulling the last {window} days")
                candidates = {}
                for q in queries:
                    await self._collect(client, site, q, window, full_refresh, candidates)
                    if self.stop_requested:
                        break
                dsem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(self._detail(client, dsem, site, name, cand, mandatory, optional,
                                                    resume_text) for cand in candidates.values()))
                if not self.stop_requested:
                    db.mark_success(site.key, sig, started)
                self.log(f"{name}: checked {len(candidates)} new posting(s)")
            except Exception as e:
                self.state["errors"].append(f"{name}: {e!r}")
                self.log(f"{name}: error {e!r}")
            finally:
                self.state["current"].remove(name)
                self.state["done"] += 1

    async def _collect(self, client, site, query, window, full_refresh, candidates):
        first = await client.list_jobs(site, query, {}, 0, 20)
        base = {}
        us = find_us_facet(first.get("facets"))
        if us:
            base = {us[0]: [us[1]]}
            first = await client.list_jobs(site, query, base, 0, 20)
        jt = find_job_type_facet(first.get("facets"))
        partitions = []
        if jt:
            param, values = jt
            partitions = [(param, vid, desc) for vid, desc, cnt in values if cnt]
            if sum(cnt for _, _, cnt in values) < (first.get("total") or 0):
                partitions.append((None, None, None))  # postings without a job type
        else:
            partitions = [(None, None, None)]

        for param, vid, wst in partitions:
            facets = dict(base)
            if param:
                facets[param] = [vid]
            offset, total, pages = 0, None, 0
            while pages < MAX_PAGES_PER_QUERY and not self.stop_requested:
                data = first if (offset == 0 and not param) else await client.list_jobs(site, query, facets, offset, 20)
                if total is None:
                    total = data.get("total") or 0
                postings = data.get("jobPostings") or []
                if not postings:
                    break
                in_window = 0
                for p in postings:
                    days = posted_days_ago(p.get("postedOn"))
                    if days is not None and days > window:
                        continue
                    in_window += 1
                    if not us and looks_non_us(p.get("locationsText")):
                        continue
                    ref = (p.get("bulletFields") or [None])[0] or p.get("externalPath")
                    job_id = f"{site.key}:{ref}"
                    if job_id in candidates:
                        continue
                    self.state["scanned"] += 1
                    if not full_refresh and db.job_exists(job_id):
                        db.touch_job(job_id)
                        continue
                    candidates[job_id] = {"id": job_id, "posting": p, "wst": wst, "days": days}
                # Without a search term Workday sorts newest-first, so we can stop at the first stale page.
                if not query and in_window == 0:
                    break
                offset += 20
                pages += 1
                if offset >= total:
                    break
            if pages >= MAX_PAGES_PER_QUERY:
                self.log(f"{site.tenant}: stopped after {pages * 20} results for one job type; "
                         f"add more mandatory keywords to narrow the search")

    async def _detail(self, client, dsem, site, company, cand, mandatory, optional, resume_text):
        if self.stop_requested:
            return
        async with dsem:
            p = cand["posting"]
            try:
                d = await client.job_detail(site, p["externalPath"])
            except Exception as e:
                self.state["errors"].append(f"{company} {p.get('title')}: {e!r}")
                return
        info = d.get("jobPostingInfo") or {}
        title = info.get("title") or p.get("title", "")
        html = info.get("jobDescription") or ""
        text = html_to_text(html)
        req_loc = info.get("jobRequisitionLocation") or {}
        locations = []
        for loc in [info.get("location"), *(info.get("additionalLocations") or []), req_loc.get("descriptor"),
                    p.get("locationsText")]:
            if loc and loc not in locations and not re.fullmatch(r"\d+ Locations", loc):
                locations.append(loc)
        country = (info.get("country") or {}).get("descriptor", "")
        alpha2 = (req_loc.get("country") or {}).get("alpha2Code", "")
        us_ok = alpha2 == "US" or "united states" in country.lower() or any(is_us_location(l) for l in locations[1:])
        if not us_ok or (mandatory and not contains_all(f"{title}\n{text}", mandatory)):
            self.state["rejected"] += 1
            return

        today = date.today()
        dates = []
        if info.get("startDate"):
            try:
                dates.append(date.fromisoformat(info["startDate"][:10]))
            except ValueError:
                pass
        if cand["days"] is not None:
            dates.append(today - timedelta(days=cand["days"]))
        posted = max(dates) if dates else today
        if (today - posted).days > RETENTION_DAYS:
            return

        us_locs = [l for l in locations if is_us_location(l) or alpha2 == "US"]
        states = sorted(set().union(*(states_in(l) for l in us_locs))) if us_locs else []
        smin, smax, stext = parse_salary(text)
        job = {
            "id": cand["id"], "company": company, "company_key": site.key, "tenant": site.tenant, "site": site.site,
            "title": title, "url": info.get("externalUrl") or f"https://{site.host}/{site.site}{p['externalPath']}",
            "external_path": p["externalPath"], "req_id": info.get("jobReqId") or (p.get("bulletFields") or [""])[0],
            "location": locations[0] if locations else "", "locations_json": locations, "states_json": states,
            "country": "United States", "remote_raw": info.get("remoteType") or "",
            "remote_type": classify_remote(info.get("remoteType"), locations, title, text),
            "employment_type": classify_employment(cand["wst"], info.get("timeType"), title, text),
            "worker_sub_type": cand["wst"] or "", "time_type": info.get("timeType") or "",
            "salary_min": smin, "salary_max": smax, "salary_text": stext,
            "posted_date": posted.isoformat(), "description_html": html, "description_text": text,
        }
        if resume_text:
            sc = scoring.score(resume_text, text, title, mandatory + optional, company)
            job.update(match_score=sc["score"], matched_json=sc["matched"], missing_json=sc["missing"])
        existed = db.job_exists(cand["id"])
        db.upsert_job(job)
        self.state["refreshed" if existed else "new_jobs"] += 1


# ---------------------------------------------------------------- scoring + view
def rescore_all():
    resume = db.get_resume()
    settings = db.get_settings()
    extra = split_keywords(settings["mandatory"]) + split_keywords(settings["optional"])
    n = 0
    for j in db.all_jobs(with_text=True):
        if resume:
            sc = scoring.score(resume["text"], j["description_text"] or "", j["title"], extra, j["company"])
            db.update_job(j["id"], match_score=sc["score"], matched=sc["matched"], missing=sc["missing"])
        else:
            db.update_job(j["id"], match_score=None, matched=[], missing=[])
        n += 1
    return n


def list_jobs(settings):
    f = settings["filters"]
    mandatory = split_keywords(settings["mandatory"])
    optional = split_keywords(settings["optional"])
    employer = settings.get("current_employer")
    wanted_types = set(f.get("types") or [])
    wanted_states = set(f.get("states") or [])
    city = (f.get("city") or "").strip().lower()
    min_salary = float(f.get("min_salary") or 0)
    aliases = {c["key"]: c["aliases"] for c in load_companies() if c["key"]} if employer else {}
    out = []
    for j in db.all_jobs():
        if j["hidden"] and not f.get("show_hidden"):
            continue
        if is_current_employer(j["company"], j["company_key"], employer, aliases.get(j["company_key"], ())):
            continue
        hay = f"{j['title']}\n{j['description_text'] or ''}"
        if mandatory and not contains_all(hay, mandatory):
            continue
        hits = keywords_present(hay, optional)
        if f.get("require_optional") and optional and not hits:
            continue
        if j["employment_type"] not in wanted_types:
            continue
        if f.get("remote_only") and j["remote_type"] != "Remote":
            continue
        if wanted_states:
            if j["states"]:
                if not wanted_states & set(j["states"]):
                    continue
            elif j["remote_type"] != "Remote":
                continue
        if city and city not in " ".join(j["locations"]).lower():
            continue
        if j["salary_max"] is None:
            if not f.get("include_no_salary", True):
                continue
        elif min_salary and j["salary_max"] < min_salary:
            continue
        j.pop("description_text", None)
        j["optional_hits"] = hits
        out.append(j)
    out.sort(key=lambda j: (-(j["match_score"] if j["match_score"] is not None else -1), -(j["salary_max"] or 0)))
    return out
