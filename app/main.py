import asyncio
import hashlib
import hmac
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import apply, db, envfile, master, scoring, search
from .config import HOME, MODELS, PORT, RETENTION_DAYS, STATIC, ensure_home, session_key
from .jobparse import split_keywords
from .resume_io import clean_resume, parse_resume, to_text
from .schemas import (CompanyIn, HideIn, OpenIn, PackageIn, PathIn, PreviewIn, ResumeSaveIn, RunIn, ScoreIn,
                      SettingsPatch, TailoredIn)

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")

WEBLLM_BUILDS = {
    "q4f16": ("Qwen2.5-0.5B-Instruct-q4f16_1-MLC", "Qwen2-0.5B-Instruct-q4f16_1_cs1k-webgpu.wasm"),
    "q4f32": ("Qwen2.5-0.5B-Instruct-q4f32_1-MLC", "Qwen2-0.5B-Instruct-q4f32_1_cs1k-webgpu.wasm"),
}

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # resume uploads

ensure_home()
SESSION_KEY = session_key()
COOKIE = f"jobagent_{PORT}"  # cookies are shared across localhost ports, so the name carries the port
HOME_ID = hashlib.sha1(str(HOME).lower().encode()).hexdigest()[:16]
runner = search.SearchRunner()


async def _purge_forever():
    while True:
        try:
            await asyncio.to_thread(db.purge_old)
        except Exception as e:  # never let one failed purge stop the hourly loop
            print(f"Job Agent: purge failed: {e!r}", flush=True)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    db.init()
    task = asyncio.create_task(_purge_forever())
    yield
    task.cancel()
    search.rescorer.wait(10)
    db.close_all()


app = FastAPI(title="Job Agent", lifespan=lifespan)

LOCKED = """<!doctype html><meta charset="utf-8"><title>Job Agent</title>
<body style="font:15px/1.5 system-ui,sans-serif;max-width:560px;margin:15vh auto;padding:0 16px">
<h1 style="font-size:20px">This Job Agent belongs to another person or profile</h1>
<p>Each person's Job Agent only opens through its own start link. Start it with <b>start.bat</b> (Windows),
<b>start.command</b> (macOS) or <b>./start.sh</b> (macOS/Linux): it opens the right page. Or copy the link printed in
that window.</p></body>"""


@app.middleware("http")
async def guard(request, call_next):
    path = request.url.path
    public = path.startswith(("/static/", "/models/")) or path == "/api/ping"  # code and model files, no personal data
    key = request.query_params.get("key")
    if public or hmac.compare_digest(request.cookies.get(COOKIE, ""), SESSION_KEY):
        response = await call_next(request)
    elif path == "/" and key and hmac.compare_digest(key, SESSION_KEY):
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, SESSION_KEY, httponly=True, samesite="strict", max_age=365 * 86400, path="/")
    elif path == "/":
        response = HTMLResponse(LOCKED, status_code=401)
    else:
        response = JSONResponse({"detail": "Open Job Agent with its start script (this is not your session)"},
                                status_code=401)
    # The start link carries the access key in ?key=; never send this page's URL to other sites.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Cross-origin isolation lets the CPU model use several threads (SharedArrayBuffer). Everything is same-origin.
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    # App JS/CSS must never be served stale after an update (vendor libs and model files may cache).
    if path.startswith("/static/") and not path.startswith("/static/vendor/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------- static + bundled model files
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _model_file(rel: str) -> Path:
    p = (MODELS / rel).resolve()
    if MODELS.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "Model file not found — run: py fetch_models.py")
    return p


@app.get("/models/webllm/libs/{name}")
def webllm_lib(name: str):
    return FileResponse(_model_file(f"webllm/libs/{name}"), media_type="application/wasm")


# web-llm expects Hugging Face style URLs: <model>/resolve/main/<file>
@app.get("/models/webllm/{model}/resolve/main/{path:path}")
def webllm_file(model: str, path: str):
    return FileResponse(_model_file(f"webllm/{model}/{path}"))


def _parts(p: Path):
    """A model file stored as <name>.part1, .part2, ... (GitHub refuses files over 100 MB), in order."""
    found = [x for x in p.parent.glob(f"{p.name}.part*") if x.name.rsplit(".part", 1)[1].isdigit()]
    return sorted(found, key=lambda x: int(x.name.rsplit(".part", 1)[1]))


@app.get("/models/onnx/{path:path}")
def onnx_file(path: str):
    p = (MODELS / "onnx" / path).resolve()
    if MODELS.resolve() in p.parents and not p.is_file() and (parts := _parts(p)):
        def joined():  # serve the parts as the one file the browser asked for
            for part in parts:
                with part.open("rb") as f:
                    while block := f.read(1 << 20):
                        yield block
        return StreamingResponse(joined(), media_type="application/octet-stream",
                                 headers={"Content-Length": str(sum(x.stat().st_size for x in parts))})
    return FileResponse(_model_file(f"onnx/{path}"))


def _bundled_models():
    webllm = {}
    for key, (mid, lib) in WEBLLM_BUILDS.items():
        if (MODELS / "webllm" / mid / "mlc-chat-config.json").exists() and (MODELS / "webllm" / "libs" / lib).exists():
            webllm[key] = {"model_id": mid, "lib": lib}
    model = MODELS / "onnx" / "Qwen2.5-0.5B-Instruct" / "onnx" / "model_quantized.onnx"
    onnx = (model.exists() or bool(_parts(model))) and (STATIC / "vendor" / "transformers" / "transformers.min.js").exists()
    return {"webllm": webllm, "onnx": onnx,
            "webllm_js": (STATIC / "vendor" / "web-llm" / "index.js").exists()}


def _sync_master():
    """Pick up outside edits of master_resume.yaml; re-scoring then runs in the background."""
    changed, error = master.sync()
    if changed:
        search.rescorer.request()
    return error


# ---------------------------------------------------------------- status / settings / companies
@app.get("/api/ping")
def ping():
    return {"app": "job-agent", "home_id": HOME_ID}


@app.get("/api/status")
def status():
    yaml_error = _sync_master()
    resume = db.get_resume()
    return {"resume": {"filename": resume["filename"], "uploaded_at": resume["uploaded_at"]} if resume else None,
            "master_error": yaml_error, "jobs": db.count_jobs(), "last_run": db.last_run_overall(),
            "rescoring": search.rescorer.running, "rescore_error": search.rescorer.last_error,
            "retention_days": RETENTION_DAYS, "models": _bundled_models(), "search": runner.state,
            "home": str(HOME), "profile": HOME.name, "account": envfile.status()}


@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: SettingsPatch):
    values = body.values()
    before = db.get_settings()
    for k in ("filters", "profile"):  # partial updates of the nested settings keep the rest
        if k in values:
            values[k] = {**before[k], **values[k]}
    db.save_settings(values)
    after = db.get_settings()
    if (before["mandatory"], before["optional"]) != (after["mandatory"], after["optional"]):
        search.rescorer.request()  # the keywords count toward the match score
    return after


@app.get("/api/companies")
def companies():
    disabled = set(db.get_settings().get("disabled_companies") or [])
    out = search.load_companies()
    for c in out:
        c["active"] = bool(c["enabled"] and c["key"] and c["key"] not in disabled)
    return out


@app.post("/api/companies")
def add_company(body: CompanyIn):
    try:
        search.append_company(body.name.strip(), body.url.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return companies()


# ---------------------------------------------------------------- master resume (master_resume.yaml)
@app.post("/api/resume/parse")
async def parse_upload(file: UploadFile = File(...)):
    """Step 1 of an upload: read the file and return a rule-based draft for the model to refine."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"That file is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB; a resume should be much smaller")
    try:
        draft = master.draft(parse_resume(file.filename, data))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Could not read that file: {e}")
    return {"filename": file.filename, "draft": draft}


@app.post("/api/resume/preview")
def preview_master(body: PreviewIn):
    """Step 2: the YAML exactly as it will be saved, for you to review (and edit) before saving."""
    try:
        note = master.note_for(body.filename or "upload", body.how or "parsed")
        return {"yaml": master.dump(body.data, note)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/resume")
def save_master(body: ResumeSaveIn):
    """Step 3 of an upload, or your own edit of master_resume.yaml. Postings are re-scored in the background."""
    try:
        data, text = master.save(yaml_text=body.yaml, filename=body.filename, new_upload=body.new_upload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    search.rescorer.request()
    return {"data": data, "yaml": text, "rescoring": True, "jobs": db.count_jobs()}


@app.get("/api/resume")
def get_resume():
    error = _sync_master()
    r = db.get_resume()
    return {"filename": r["filename"] if r else None, "data": r["data"] if r else None,
            "yaml": master.read_yaml(), "error": error}


# ---------------------------------------------------------------- search
@app.post("/api/search/run")
async def run_search(body: RunIn | None = None):
    await asyncio.to_thread(_sync_master)
    try:
        runner.start(bool(body and body.full_refresh))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return runner.state


@app.post("/api/search/stop")
def stop_search():
    runner.stop()
    return runner.state


@app.get("/api/search/status")
def search_status():
    return runner.state


# ---------------------------------------------------------------- jobs
@app.get("/api/jobs")
def jobs():
    return search.list_jobs(db.get_settings())


def _job_or_404(job_id):
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found (untouched postings are purged after 7 days)")
    return j


def _score(j, text):
    settings = db.get_settings()
    extra = split_keywords(settings["mandatory"]) + split_keywords(settings["optional"])
    return scoring.score(text, j["description_text"] or "", j["title"], extra, j["company"])


@app.get("/api/jobs/{job_id:path}/detail")
def job_detail(job_id: str):
    j = _job_or_404(job_id)
    resume = db.get_resume()
    j["analysis"] = _score(j, resume["text"] if resume else "")
    j["tailored"] = db.get_tailored(job_id)
    return j


@app.post("/api/jobs/{job_id:path}/hide")
def hide_job(job_id: str, body: HideIn | None = None):
    _job_or_404(job_id)
    db.update_job(job_id, hidden=1 if (body is None or body.hidden) else 0)
    return {"ok": True}


@app.post("/api/jobs/{job_id:path}/score")
def score_resume(job_id: str, body: ScoreIn):
    return _score(_job_or_404(job_id), to_text(clean_resume(body.resume)))


@app.put("/api/jobs/{job_id:path}/tailored")
def save_tailored(job_id: str, body: TailoredIn):
    _job_or_404(job_id)
    db.save_tailored(job_id, body.doc, body.score_after, body.approved, body.rejected)
    j = db.get_job(job_id)
    if j["status"] in (None, "new"):
        db.update_job(job_id, status="tailored")
    return {"ok": True}


@app.post("/api/jobs/{job_id:path}/package")
async def package(job_id: str, body: PackageIn):
    """Save the application folder; with launch=true also open the autofill browser."""
    j = _job_or_404(job_id)
    settings = db.get_settings()
    profile = settings["profile"]
    doc = body.doc
    db.save_tailored(job_id, doc, body.score_after, body.approved, body.rejected)
    result = await apply.save_package(j, doc["resume"], j.get("match_score"), body.score_after,
                                      body.approved, body.rejected, profile)
    db.update_job(job_id, folder=result["folder"], status="applied" if j["status"] == "applied" else "saved")
    if body.launch:
        fmt = settings.get("upload_format", "docx")
        resume_path = result["pdf"] if fmt == "pdf" and result["pdf"] else result["docx"]
        try:
            await apply.worker.open_application(job_id, j["url"], profile, resume_path, result["folder"], j["tenant"])
            db.update_job(job_id, status="applied" if j["status"] == "applied" else "applying")
            result["launched"] = True
        except Exception as e:
            result["launched"] = False
            result["launch_error"] = str(e)
    return result


@app.post("/api/jobs/{job_id:path}/applied")
def mark_applied(job_id: str):
    j = _job_or_404(job_id)
    apply.mark_applied(job_id, j.get("folder"), how="manual")
    return {"ok": True}


# ---------------------------------------------------------------- personal folder
@app.get("/api/applications")
def applications():
    return apply.list_applications()


@app.post("/api/open-folder")
def open_folder(body: PathIn):
    try:
        apply.open_folder(body.path)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/open")
def open_personal(body: OpenIn):
    try:
        apply.open_personal(body.what)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
