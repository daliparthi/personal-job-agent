"""
Application packages + browser automation.

* save_package(): writes <personal folder>/Applications/<Company>/<Title - ReqID>/ with the tailored resume
  (DOCX/PDF/TXT/YAML), the job description (HTML/TXT) and application.json.
* BrowserWorker: runs Playwright in its own thread. It renders PDFs (headless) and opens a visible Chrome/Edge
  window with your personal browser profile for applying. autofill.js fills standard fields, the account loop
  types your .env email/password into Workday's Create Account / Sign In form; you click every button yourself.
"""
import asyncio
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import db, envfile
from .config import APPLICATIONS, BROWSER_PROFILE, ENV_FILE, HOME, MASTER_YAML
from .master import TAILORED_HEADER, dump
from .resume_io import clean_resume, to_docx, to_html, to_text
from .workday import is_workday_host

AUTOFILL_JS = (Path(__file__).parent / "autofill.js").read_text(encoding="utf-8")


def browser_candidates():
    """Installed Chrome / Edge first (any OS), then a Chromium-family browser on PATH or in /Applications
    (Linux often has only Chromium), then Playwright's own Chromium if `playwright install chromium` was run."""
    yield "Chrome", {"channel": "chrome"}
    yield "Edge", {"channel": "msedge"}
    paths = [shutil.which(n) for n in ("chromium", "chromium-browser", "brave-browser")]
    if sys.platform == "darwin":
        paths += [f"/Applications/{a}.app/Contents/MacOS/{b}"
                  for a, b in (("Chromium", "Chromium"), ("Brave Browser", "Brave Browser"))]
    for p in paths:
        if p and os.path.exists(p):
            yield p, {"executable_path": p}
    yield "Playwright Chromium", {}


def trusted_frame(frame) -> bool:
    """A page-binding call may come from any frame of any site; trust only a top frame on a Workday host."""
    if frame is None or getattr(frame, "parent_frame", None) is not None:
        return False
    try:
        return is_workday_host(urlsplit(frame.url).hostname)
    except Exception:
        return False


def safe_name(s: str, limit=80) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", s or "").strip(" .")
    return re.sub(r"\s+", " ", s)[:limit].strip(" .") or "Untitled"


class BrowserWorker:
    def __init__(self):
        self.loop = None
        self.thread = None
        self._ready = threading.Event()
        self.pw = None
        self.headless = None
        self.ctx = None
        self.pages = {}  # Page -> {"job_id", "profile", "folder"}

    # ------------------------------------------------------------ thread plumbing
    def _ensure_thread(self):
        if self.thread:
            return

        def run():
            loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            self._ready.set()
            loop.run_forever()

        self.thread = threading.Thread(target=run, name="playwright", daemon=True)
        self.thread.start()
        self._ready.wait()

    async def call(self, coro_fn, *args):
        self._ensure_thread()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args), self.loop)
        return await asyncio.wrap_future(fut)

    async def _playwright(self):
        if not self.pw:
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()
        return self.pw

    async def _launch(self, fn, **kw):
        errors = []
        for name, opts in browser_candidates():
            try:
                return await fn(**opts, **kw)
            except Exception as e:  # not installed — try the next one
                errors.append(f"{name}: {str(e).strip().splitlines()[0][:160]}")
        raise RuntimeError("Could not start Chrome, Edge or Chromium. Install one of them, or run "
                           f"'{sys.executable} -m playwright install chromium'. Details: " + " / ".join(errors))

    # ------------------------------------------------------------ PDF
    async def _pdf(self, html, out_path):
        pw = await self._playwright()
        if not self.headless or not self.headless.is_connected():
            self.headless = await self._launch(pw.chromium.launch, headless=True)
        page = await self.headless.new_page()
        try:
            await page.set_content(html, wait_until="load")
            await page.pdf(path=str(out_path), format="Letter", print_background=True,
                           margin={"top": "0.55in", "bottom": "0.55in", "left": "0.6in", "right": "0.6in"})
        finally:
            await page.close()

    async def render_pdf(self, html, out_path):
        return await self.call(self._pdf, html, out_path)

    # ------------------------------------------------------------ applying
    async def _context(self):
        if self.ctx:
            try:
                _ = self.ctx.pages
                await self.ctx.cookies()
                return self.ctx
            except Exception:
                self.ctx = None
        pw = await self._playwright()
        BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
        self.ctx = await self._launch(pw.chromium.launch_persistent_context, user_data_dir=str(BROWSER_PROFILE),
                                      headless=False, no_viewport=True, bypass_csp=True,
                                      args=["--start-maximized"])
        await self.ctx.expose_binding("__jobAgentEvent", self._on_event)
        await self.ctx.add_init_script(script=AUTOFILL_JS)
        self.ctx.on("close", lambda *_: setattr(self, "ctx", None))
        return self.ctx

    async def _on_event(self, source, kind, payload=None):
        # The binding exists on every page of this browser profile. Answer only the Workday page we opened, and
        # only while its top frame is on a Workday host (not after it navigates to a sign-on or any other site).
        info = self.pages.get(source.get("page"))
        if not info or not trusted_frame(source.get("frame")):
            return None
        if kind == "profile":
            return info["profile"]
        if kind == "applied":
            mark_applied(info["job_id"], info["folder"], how=str(payload or "manual"))
            return True
        return None

    async def _open(self, job_id, url, profile, resume_path, folder, account):
        ctx = await self._context()
        page = await ctx.new_page()
        page_profile = {**profile, "email": profile.get("email") or account["email"],
                        "account_ready": bool(account["email"] and account["password"])}
        self.pages[page] = {"job_id": job_id, "profile": page_profile, "folder": folder}
        page.on("close", lambda p: self.pages.pop(p, None))
        await page.goto(url, wait_until="domcontentloaded")
        await page.bring_to_front()
        try:  # the posting's own "Apply" button — opens Workday's "Start your application" choices
            await page.locator('[data-automation-id="adventureButton"]').first.click(timeout=15000)
        except Exception:
            pass
        asyncio.ensure_future(self._upload_loop(page, urlsplit(url).hostname, resume_path))
        asyncio.ensure_future(self._account_loop(page, urlsplit(url).hostname, account))
        return True

    async def _account_loop(self, page, host, account):
        """Type the .env email/password into Workday's Create Account / Sign In form. Never clicks anything.

        Only on the posting's own Workday host (never on a single-sign-on or any other site), only into empty
        fields, and once per form so it never fights you if you change what it typed."""
        if not account["password"] and not account["email"]:
            return
        done = set()
        while not page.is_closed():
            try:
                if urlsplit(page.url).hostname == host:
                    passwords = page.locator('input[type="password"]:visible')
                    n = await passwords.count()
                    key = (page.url.split("?")[0].split("#")[0], n)
                    if n and key not in done:
                        email = page.locator('input[data-automation-id="email"]:visible, input[type="email"]:visible, '
                                             'input[autocomplete="email"]:visible, input[autocomplete="username"]:visible')
                        if account["email"] and await email.count() and not await email.first.input_value():
                            await email.first.fill(account["email"])
                        if account["password"]:
                            for i in range(n):  # Create Account has Password + Verify Password
                                field = passwords.nth(i)
                                if not await field.input_value():
                                    await field.fill(account["password"])
                        done.add(key)
            except Exception:
                pass
            await asyncio.sleep(1.5)

    async def _upload_loop(self, page, host, resume_path):
        """Attach the tailored resume whenever Workday shows a resume upload control.

        Only on the posting's own Workday host (or another Workday career-site host), never on any other site."""
        done = set()
        while not page.is_closed():
            try:
                current = urlsplit(page.url).hostname
                if current == host or is_workday_host(current):
                    inputs = page.locator('input[type="file"][data-automation-id="file-upload-input-ref"]')
                    if not await inputs.count():
                        inputs = page.locator('input[type="file"]')
                    existing = await page.locator('[data-automation-id="file-upload-item"], '
                                                  '[data-automation-id="fileUploadItem"]').count()
                    key = page.url.split("?")[0]
                    if await inputs.count() and not existing and key not in done:
                        await inputs.first.set_input_files(str(resume_path))
                        done.add(key)
            except Exception:
                pass
            await asyncio.sleep(2)

    async def open_application(self, job_id, url, profile, resume_path, folder, tenant):
        account = envfile.account_for(tenant)
        return await self.call(self._open, job_id, url, profile, resume_path, folder, account)


worker = BrowserWorker()


# ---------------------------------------------------------------- packages on disk
def _jd_html(job):
    salary = ""
    if job.get("salary_max"):
        salary = f"${job['salary_min']:,.0f} – ${job['salary_max']:,.0f} / yr"
    meta = [("Company", job["company"]), ("Location", "; ".join(job.get("locations") or [job.get("location", "")])),
            ("Job type", job.get("employment_type")), ("Work mode", job.get("remote_type")),
            ("Salary", salary or "not listed"), ("Posted", job.get("posted_date")),
            ("Requisition", job.get("req_id")), ("Posting URL", job.get("url"))]
    rows = "".join(f"<tr><th>{htmllib.escape(k)}</th><td>{htmllib.escape(str(v or ''))}</td></tr>" for k, v in meta)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{htmllib.escape(job['title'])}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:860px;margin:24px auto;line-height:1.5;color:#111;padding:0 16px}}
th{{text-align:left;padding-right:16px;color:#555;font-weight:600}}table{{margin-bottom:20px}}</style></head>
<body><h1>{htmllib.escape(job['title'])}</h1><table>{rows}</table><hr>{job.get('description_html') or ''}</body></html>"""


def _resume_basename(profile):
    first, last = (profile.get("first_name") or "").strip(), (profile.get("last_name") or "").strip()
    return safe_name(f"{first}_{last}_Resume") if first or last else "Tailored_Resume"


async def save_package(job, resume, score_before, score_after, approved, rejected, profile):
    folder = APPLICATIONS / safe_name(job["company"], 60) / safe_name(f"{job['title']} - {job.get('req_id') or ''}", 90)
    folder.mkdir(parents=True, exist_ok=True)
    resume = clean_resume(resume)
    base = _resume_basename(profile)
    docx_path = folder / f"{base}.docx"
    pdf_path = folder / f"{base}.pdf"
    to_docx(resume, docx_path)
    (folder / f"{base}.txt").write_text(to_text(resume), encoding="utf-8")
    (folder / "tailored_resume.yaml").write_text(
        dump(resume, f"Tailored for {job['title']} at {job['company']} ({job.get('req_id') or 'no req ID'}).", TAILORED_HEADER),
        encoding="utf-8")
    pdf_error = None
    try:
        await worker.render_pdf(to_html(resume, title=base), pdf_path)
    except Exception as e:
        pdf_error = str(e)
    (folder / "job_description.html").write_text(_jd_html(job), encoding="utf-8")
    (folder / "job_description.txt").write_text(f"{job['title']}\n{job['company']}\n{job.get('url')}\n\n"
                                                f"{job.get('description_text') or ''}", encoding="utf-8")
    meta_path = folder / "application.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta.update({
        "job_id": job["id"], "company": job["company"], "title": job["title"], "req_id": job.get("req_id"),
        "url": job.get("url"), "location": job.get("locations"), "employment_type": job.get("employment_type"),
        "work_mode": job.get("remote_type"), "salary_min": job.get("salary_min"), "salary_max": job.get("salary_max"),
        "posted_date": job.get("posted_date"), "match_score_before": score_before, "match_score_after": score_after,
        "approved_keywords": approved, "rejected_keywords": rejected,
        "status": meta.get("status") if meta.get("status") == "applied" else "prepared",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "files": {"resume_docx": docx_path.name, "resume_pdf": pdf_path.name if not pdf_error else None,
                  "resume_yaml": "tailored_resume.yaml", "job_description": "job_description.html"},
    })
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"folder": str(folder), "docx": str(docx_path), "pdf": None if pdf_error else str(pdf_path),
            "pdf_error": pdf_error}


def mark_applied(job_id, folder, how="manual"):
    if folder:
        meta_path = Path(folder) / "application.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.update(status="applied", applied_at=datetime.now().isoformat(timespec="seconds"), applied_how=how)
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if db.get_job(job_id):
        db.update_job(job_id, status="applied")


def list_applications():
    out = []
    for meta_path in APPLICATIONS.glob("*/*/application.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta["folder"] = str(meta_path.parent)
        out.append(meta)
    out.sort(key=lambda m: m.get("applied_at") or m.get("prepared_at") or "", reverse=True)
    return out


def _reveal(p: Path, text=False):
    """Open a folder in the file manager, or (text=True) a file in a text editor — on Windows, macOS or Linux."""
    if sys.platform == "win32":
        if text:
            subprocess.Popen(["notepad.exe", str(p)])  # .env / .yaml often have no default app on Windows
        else:
            os.startfile(p)  # noqa: S606 - local desktop app
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-t", str(p)] if text else ["open", str(p)])  # -t: the default text editor
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise OSError(f"No desktop opener (xdg-open) found. Open it yourself: {p}")
        subprocess.Popen([opener, str(p)])


def open_folder(path: str):
    p = Path(path).resolve()
    if APPLICATIONS.resolve() not in p.parents and p != APPLICATIONS.resolve():
        raise ValueError("Only folders under Applications/ can be opened")
    _reveal(p)


def open_personal(what: str):
    """Open your personal folder, or your .env / master_resume.yaml in a text editor."""
    if what == "home":
        return _reveal(HOME)
    target = {"env": ENV_FILE, "master": MASTER_YAML}.get(what)
    if not target or not target.exists():
        raise ValueError("Nothing to open")
    _reveal(target, text=True)
