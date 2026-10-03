"""Search orchestration: incremental/full pulls across the company lists, plus the filtered/sorted job view."""
import asyncio
import copy
import hashlib
import json
import re
import threading
from datetime import date, datetime, timedelta

import httpx
import yaml

from . import alerts, candidate, db, pipeline, scoring
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


_companies_cache = {"key": None, "value": None}
_companies_lock = threading.Lock()


def _mtime(path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def load_companies():
    """Both company lists merged. Re-read only when either file changed (it is called for every job list)."""
    _migrate_old_copy()
    key = (str(COMPANIES_SHARED), _mtime(COMPANIES_SHARED), str(MY_COMPANIES), _mtime(MY_COMPANIES))
    with _companies_lock:
        if _companies_cache["key"] != key:
            _companies_cache.update(key=key, value=_load_companies())
        return copy.deepcopy(_companies_cache["value"])


def _load_companies():
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
FLUSH_EVERY = 20  # postings written to the database per transaction during a search
CHECK_TRACKED_EVERY_HOURS = 20  # how often a search re-checks that a job you track is still posted


class SearchRunner:
    """One search at a time, as an asyncio task on the server's event loop.

    Everything slow that isn't a network call (SQLite, scoring, reading the company files) runs in a worker thread
    via asyncio.to_thread, so the page stays responsive while a search runs."""

    def __init__(self):
        self.task = None
        self.stop_requested = False
        self.spec = None      # the saved search being run, or None for the keywords in Settings
        self.new_ids = []     # postings first stored by this run (alerts are made from these)
        self.state = self._blank()

    @staticmethod
    def _blank():
        return {"running": False, "mode": None, "trigger": None, "search": None, "started_at": None,
                "finished_at": None, "total": 0, "done": 0, "current": [], "scanned": 0, "new_jobs": 0,
                "refreshed": 0, "rejected": 0, "alerts": 0, "log": [], "errors": []}

    def log(self, msg):
        self.state["log"].append(f"{datetime.now():%H:%M:%S}  {msg}")
        self.state["log"] = self.state["log"][-300:]

    def start(self, full_refresh: bool, spec: dict | None = None, trigger: str = "manual"):
        """spec: a saved search ({id, name, mandatory, optional, notify_min_score}); None = the Settings keywords.
        trigger: manual (Run button), schedule (the server's scheduler) or headless (run.py --run-searches)."""
        if self.state["running"]:
            raise RuntimeError("A search is already running")
        self.stop_requested = False
        self.spec = spec
        self.new_ids = []
        self.state = self._blank()
        self.state.update(running=True, mode="full refresh" if full_refresh else "incremental", trigger=trigger,
                          search=spec["name"] if spec else None,
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
            settings = await asyncio.to_thread(db.get_settings)
            source = self.spec or settings
            mandatory = split_keywords(source["mandatory"])
            optional = split_keywords(source["optional"])
            if self.spec:
                self.log(f"Saved search \"{self.spec['name']}\"")
            if not mandatory and not optional:
                self.log("Enter at least one mandatory or optional keyword before searching.")
                return
            keep_days = settings.get("keep_new_days") or RETENTION_DAYS
            purged = await asyncio.to_thread(db.purge_old, keep_days)
            if purged:
                self.log(f"Purged {purged} untouched postings older than {keep_days} days")
            resume = await asyncio.to_thread(db.get_resume)
            ctx = {"mandatory": mandatory, "optional": optional, "full_refresh": full_refresh,
                   "sig": keyword_signature(mandatory, optional), "kw_sig": keyword_hits_signature(mandatory, optional),
                   "resume_text": resume["text"] if resume else "",
                   "profile": candidate.profile(resume["data"], settings["profile"]) if resume else None}
            disabled = set(settings.get("disabled_companies") or [])
            companies = []
            for c in await asyncio.to_thread(load_companies):
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
            ctx["queries"] = [" ".join(mandatory)] if mandatory else optional
            self.log(f"{self.state['mode'].title()} run over {len(companies)} companies; "
                     f"Workday search: {' | '.join(repr(q) for q in ctx['queries'])}")
            sem = asyncio.Semaphore(COMPANY_CONCURRENCY)
            await asyncio.gather(*(self._company(client, sem, c, ctx) for c in companies))
            self.log(f"Done. {self.state['new_jobs']} new, {self.state['refreshed']} refreshed, "
                     f"{self.state['rejected']} dropped by keyword/location checks.")
        except Exception as e:  # surface anything unexpected in the UI
            self.state["errors"].append(f"Search failed: {e!r}")
            self.log(f"Search failed: {e!r}")
        finally:
            await client.close()
            self.state["current"] = []
            self.state["finished_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                self.state["alerts"] = await asyncio.to_thread(self._finish)
            except Exception as e:
                self.state["errors"].append(f"Saving the run failed: {e!r}")
            self.state["running"] = False

    def _finish(self) -> int:
        """Record the run in the history, mark the saved search as run, and raise alerts for strong new matches
        (scheduled and headless runs of a saved search with an alert threshold). Returns the number of alerts."""
        st, spec = self.state, self.spec
        db.record_run({"search_id": spec and spec.get("id"), "search_name": spec and spec.get("name"), "mode": st["mode"],
                       "trigger": st["trigger"], "started": st["started_at"], "finished": st["finished_at"],
                       "new_jobs": st["new_jobs"], "refreshed": st["refreshed"], "rejected": st["rejected"],
                       "errors": st["errors"], "log_text": "\n".join(st["log"])})
        if not spec or not spec.get("id"):
            return 0
        db.mark_search_ran(spec["id"], st["started_at"])
        threshold = spec.get("notify_min_score")
        if threshold is None or st["trigger"] == "manual" or not self.new_ids:
            return 0
        rows = []
        for job_id in self.new_ids:
            job = db.get_job(job_id)
            if job and job.get("match_score") is not None and job["match_score"] >= threshold:
                rows.append((job_id, spec["id"], job["match_score"]))
        made = db.add_alerts(rows) if rows else 0
        if made:
            alerts.write_digest()
            self.log(f"{made} new match(es) scored {threshold} or more")
        return made

    async def _company(self, client, sem, comp, ctx):
        async with sem:
            if self.stop_requested:
                return
            name = comp["name"]
            self.state["current"].append(name)
            started = datetime.now()
            try:
                site = parse_site(name, comp["url"])
                last = None if ctx["full_refresh"] else await asyncio.to_thread(db.last_success, site.key, ctx["sig"])
                if last:
                    window = min(RETENTION_DAYS, (date.today() - last.date()).days + 1)
                    self.log(f"{name}: incremental — postings from the last {window} day(s)")
                else:
                    window = RETENTION_DAYS
                    self.log(f"{name}: pulling the last {window} days")
                known = await asyncio.to_thread(db.job_ids, site.key)
                candidates, seen = {}, set()
                for q in ctx["queries"]:
                    await self._collect(client, site, q, window, ctx["full_refresh"], candidates, known, seen)
                    if self.stop_requested:
                        break
                if seen:
                    await asyncio.to_thread(db.touch_jobs, sorted(seen))
                pending = []
                dsem = asyncio.Semaphore(DETAIL_CONCURRENCY)
                await asyncio.gather(*(self._detail(client, dsem, site, name, cand, ctx, known, pending)
                                       for cand in candidates.values()))
                await self._flush(pending, force=True)
                if not self.stop_requested:
                    await asyncio.to_thread(db.mark_success, site.key, ctx["sig"], started)
                self.log(f"{name}: checked {len(candidates)} new posting(s)")
                await self._check_tracked(client, site, name)
            except Exception as e:
                self.state["errors"].append(f"{name}: {e!r}")
                self.log(f"{name}: error {e!r}")
            finally:
                self.state["current"].remove(name)
                self.state["done"] += 1

    async def _check_tracked(self, client, site, name):
        """Is each job you are working on or applied to (at this company) still posted? Checked at most daily."""
        since = (datetime.now() - timedelta(hours=CHECK_TRACKED_EVERY_HOURS)).isoformat(timespec="seconds")
        rows = await asyncio.to_thread(db.tracked_to_check, site.key, pipeline.CHECK_STILL_POSTED, since)
        for r in rows:
            if self.stop_requested:
                return
            try:
                d = await client.job_detail(site, r["external_path"])
                closed = not d.get("jobPostingInfo")
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (404, 410):
                    continue  # a server hiccup is not a closed posting; try again next time
                closed = True
            except Exception:
                continue
            await asyncio.to_thread(pipeline.record_check, r["id"], closed)
            if closed:
                self.log(f"{name}: '{r['title']}' is no longer posted")

    @staticmethod
    async def _flush(pending, force=False):
        """Write finished postings in batches (one transaction each) so they appear in the list as the search goes."""
        if pending and (force or len(pending) >= FLUSH_EVERY):
            batch = pending[:]
            pending.clear()
            await asyncio.to_thread(db.upsert_jobs, batch)

    async def _collect(self, client, site, query, window, full_refresh, candidates, known, seen):
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
                    if job_id in candidates or job_id in seen:
                        continue
                    self.state["scanned"] += 1
                    if not full_refresh and job_id in known:
                        seen.add(job_id)  # still listed: refresh last_seen, no detail request
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

    async def _detail(self, client, dsem, site, company, cand, ctx, known, pending):
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
        mandatory, optional = ctx["mandatory"], ctx["optional"]
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
            "kw_sig": ctx["kw_sig"], "mandatory_ok": 1,
            "optional_hits_json": keywords_present(f"{title}\n{text}", optional),
        }
        if ctx["resume_text"]:
            sc = await asyncio.to_thread(scoring.score, ctx["resume_text"], text, title, mandatory + optional, company,
                                         ctx["profile"])
            job.update(match_score=sc["score"], matched_json=sc["matched"], missing_json=sc["missing"],
                       knockouts_json=blocking_knockouts(sc))
        pending.append(job)
        if cand["id"] in known:
            self.state["refreshed"] += 1
        else:
            self.state["new_jobs"] += 1
            self.new_ids.append(cand["id"])
        await self._flush(pending)


# ---------------------------------------------------------------- scoring + view
def blocking_knockouts(sc) -> list:
    """What the job list keeps of a score: the hard requirements you don't meet ([{kind, label}])."""
    return [{"kind": k["kind"], "label": k["label"]} for k in sc.get("knockouts") or [] if k["blocking"]]


def rescore_all():
    """Re-score every stored posting against the master resume, in one transaction. Returns the number of jobs."""
    resume = db.get_resume()
    settings = db.get_settings()
    extra = split_keywords(settings["mandatory"]) + split_keywords(settings["optional"])
    prof = candidate.profile(resume["data"], settings["profile"]) if resume else None
    scores = []
    for j in db.all_jobs(with_text=True):
        if resume:
            sc = scoring.score(resume["text"], j["description_text"] or "", j["title"], extra, j["company"], prof)
            scores.append((j["id"], sc["score"], sc["matched"], sc["missing"], blocking_knockouts(sc)))
        else:
            scores.append((j["id"], None, [], [], []))
    db.set_scores(scores)
    return len(scores)


class Rescorer:
    """Runs rescore_all() in a background thread. A request made while it runs queues one more pass afterwards,
    so the latest change always wins and two passes never run at once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = False
        self._idle = threading.Event()
        self._idle.set()
        self.running = False
        self.last_error = None

    def request(self):
        with self._lock:
            self._pending = True
            if self.running:
                return
            self.running = True
            self._idle.clear()
        threading.Thread(target=self._loop, name="rescore", daemon=True).start()

    def _loop(self):
        while True:
            with self._lock:
                if not self._pending:
                    self.running = False
                    self._idle.set()
                    return
                self._pending = False
            try:
                rescore_all()
                self.last_error = None
            except Exception as e:  # shown in /api/status; the next request tries again
                self.last_error = repr(e)

    def wait(self, timeout=None) -> bool:
        """Block until no pass is running or queued (tests, shutdown)."""
        return self._idle.wait(timeout)


rescorer = Rescorer()


def keyword_hits_signature(mandatory, optional) -> str:
    """Identifies the keyword set a job's stored mandatory/optional check was made for."""
    return hashlib.sha1(json.dumps([mandatory, optional]).encode()).hexdigest()[:16]


def refresh_keyword_hits(mandatory, optional) -> int:
    """Re-check the keywords only for jobs stored under a different keyword set. Returns how many were updated."""
    sig = keyword_hits_signature(mandatory, optional)
    rows = db.jobs_needing_keywords(sig)
    if rows:
        hits = []
        for r in rows:
            hay = f"{r['title']}\n{r['description_text'] or ''}"
            hits.append((r["id"], contains_all(hay, mandatory), keywords_present(hay, optional)))
        db.set_keyword_hits(sig, hits)
    return len(rows)


def list_jobs(settings):
    f = settings["filters"]
    mandatory = split_keywords(settings["mandatory"])
    optional = split_keywords(settings["optional"])
    employer = settings.get("current_employer")
    wanted_types = list(f.get("types") or [])
    if not wanted_types:
        return []
    wanted_states = set(f.get("states") or [])
    city = (f.get("city") or "").strip().lower()
    refresh_keyword_hits(mandatory, optional)
    # Postings from the keep window, plus older ones you are still preparing; applied ones live on the pipeline board.
    posted_since = (date.today() - timedelta(days=settings.get("keep_new_days") or RETENTION_DAYS)).isoformat()
    rows = db.jobs_for_list(wanted_types, show_hidden=bool(f.get("show_hidden")), remote_only=bool(f.get("remote_only")),
                            min_salary=float(f.get("min_salary") or 0),
                            include_no_salary=f.get("include_no_salary", True) is not False,
                            kw_sig=keyword_hits_signature(mandatory, optional), posted_since=posted_since,
                            hide_knockouts=bool(f.get("hide_knockouts")))
    aliases = {c["key"]: c["aliases"] for c in load_companies() if c["key"]} if employer else {}
    out = []
    for j in rows:
        if is_current_employer(j["company"], j["company_key"], employer, aliases.get(j["company_key"], ())):
            continue
        if f.get("require_optional") and optional and not j["optional_hits"]:
            continue
        if wanted_states:
            if j["states"]:
                if not wanted_states & set(j["states"]):
                    continue
            elif j["remote_type"] != "Remote":
                continue
        if city and city not in " ".join(j["locations"]).lower():
            continue
        out.append(j)
    return out  # already sorted by the query: best match first, then salary
