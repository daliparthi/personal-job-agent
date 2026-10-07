from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import apply, config, db, main, master, search
from tests.conftest import SAMPLE_MASTER, SAMPLE_RESUME, make_job

JOB = "acme/external:R1"
JOB_URL = "/api/jobs/" + "/".join(quote(p, safe=":") for p in JOB.split("/"))


@pytest.fixture
def fake_pdf(monkeypatch):
    async def render(html, out_path):
        out_path.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(apply.worker, "render_pdf", render)


# ---------------------------------------------------------------- access guard
def test_guard_blocks_without_the_start_link(anon):
    assert anon.get("/api/status").status_code == 401
    r = anon.get("/")
    assert r.status_code == 401 and "belongs to another person" in r.text
    assert anon.get("/?key=wrong").status_code == 401
    assert anon.get("/api/ping").json()["app"] == "job-agent"           # public: no personal data
    assert anon.get("/static/app.css").status_code == 200               # public: program files
    assert anon.get("/static/app.css").headers["cross-origin-embedder-policy"] == "require-corp"


def test_start_link_sets_a_strict_cookie(anon):
    r = anon.get(f"/?key={main.SESSION_KEY}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert anon.get("/api/status").status_code == 200
    assert anon.get("/").status_code == 200


# ---------------------------------------------------------------- status, settings, companies
def test_status_and_settings(client):
    st = client.get("/api/status").json()
    assert st["resume"] is None and st["jobs"] == 0 and st["profile"] == config.HOME.name
    assert "password" not in st["account"]  # only flags such as password_set, never a password
    s = client.put("/api/settings", json={"mandatory": "python", "filters": {"remote_only": True}}).json()
    assert s["mandatory"] == "python" and s["filters"]["remote_only"] is True and s["filters"]["include_no_salary"]


def test_search_keywords_do_not_change_the_score(client):
    db.upsert_job(make_job())
    master.save(data=SAMPLE_MASTER, filename="cv.txt")  # writes master_resume.yaml, which /api/status syncs from
    client.get("/api/status")
    search.rescore_all()
    first = db.get_job(JOB)["match_score"]
    assert first is not None
    client.put("/api/settings", json={"mandatory": "Python", "optional": "Frobnicator, Kafka"})
    assert search.rescorer.wait(10)
    search.rescore_all()
    assert db.get_job(JOB)["match_score"] == first
    assert client.post(f"{JOB_URL}/score", json={"resume": SAMPLE_MASTER}).json()["score"] is not None
    assert client.get("/api/status").json()["rescoring"] is False


def test_applicant_change_rescores(client):
    db.upsert_job(make_job(description_text="We use Python and SQL on AWS. We cannot sponsor visas."))
    master.save(data=SAMPLE_MASTER, filename="cv.txt")
    client.get("/api/status")
    search.rescore_all()
    before = db.get_job(JOB)["match_score"]
    client.put("/api/settings", json={"profile": {"needs_sponsorship": "Yes"}})
    assert search.rescorer.wait(10)
    assert db.get_job(JOB)["match_score"] < before


def test_settings_patch_merges_nested_values(client):
    client.put("/api/settings", json={"filters": {"remote_only": True}, "profile": {"first_name": "Jordan"}})
    s = client.put("/api/settings", json={"filters": {"city": "Austin"}, "profile": {"last_name": "Avery"}}).json()
    assert s["filters"]["remote_only"] is True and s["filters"]["city"] == "Austin"
    assert s["profile"]["first_name"] == "Jordan" and s["profile"]["last_name"] == "Avery"


@pytest.mark.parametrize("body", [{"max_bullets": 99}, {"engine": "gpu"}, {"filters": {"min_salary": -1}},
                                  {"upload_format": "rtf"}])
def test_settings_validation(client, body):
    r = client.put("/api/settings", json=body)
    assert r.status_code == 422 and r.json()["detail"]


def test_bodies_are_validated(client):
    db.upsert_job(make_job())
    assert client.put(f"{JOB_URL}/tailored", json={"doc": {"changes": {}}}).status_code == 422
    assert client.post(f"{JOB_URL}/score", json={}).status_code == 422
    assert client.post("/api/search/run", json={"full_refresh": "maybe"}).status_code == 422


def test_add_company(client):
    r = client.post("/api/companies", json={"name": "Initech", "url": "https://initech.wd1.myworkdayjobs.com/Jobs"})
    assert r.status_code == 200 and any(c["name"] == "Initech" and c["active"] for c in r.json())
    r = client.post("/api/companies", json={"name": "Nope", "url": "https://example.com"})
    assert r.status_code == 400


# ---------------------------------------------------------------- master resume upload flow
def test_resume_upload_preview_save(client):
    r = client.post("/api/resume/parse", files={"file": (SAMPLE_RESUME.name, SAMPLE_RESUME.read_bytes(), "text/plain")})
    draft = r.json()["draft"]
    assert draft["name"] == "Jordan Avery"
    yaml_text = client.post("/api/resume/preview", json={"data": draft, "filename": "cv.txt", "how": "test"}).json()["yaml"]
    assert "Built " in yaml_text and "from cv.txt (test)" in yaml_text
    saved = client.put("/api/resume", json={"yaml": yaml_text, "filename": "cv.txt", "new_upload": True}).json()
    assert saved["data"]["name"] == "Jordan Avery"
    got = client.get("/api/resume").json()
    assert got["filename"] == "cv.txt" and got["yaml"] == yaml_text and got["error"] is None
    assert client.get("/api/status").json()["resume"]["filename"] == "cv.txt"


def test_resume_errors(client):
    assert client.post("/api/resume/parse", files={"file": ("cv.rtf", b"x", "text/rtf")}).status_code == 400
    assert client.put("/api/resume", json={"yaml": "name: [oops"}).status_code == 400


# ---------------------------------------------------------------- jobs, tailoring, packaging
def test_job_lifecycle(client, fake_pdf):
    db.upsert_job(make_job())
    db.save_resume("cv", "Python SQL", SAMPLE_MASTER, 0)
    assert [j["id"] for j in client.get("/api/jobs").json()] == [JOB]
    d = client.get(f"{JOB_URL}/detail").json()
    assert d["analysis"]["score"] >= 0 and d["tailored"] is None
    assert client.get("/api/jobs/acme/external:nope/detail").status_code == 404

    sc = client.post(f"{JOB_URL}/score", json={"resume": SAMPLE_MASTER}).json()
    doc = {"resume": SAMPLE_MASTER, "changes": {}}
    client.put(f"{JOB_URL}/tailored", json={"doc": doc, "score_after": sc["score"], "approved": [], "rejected": []})
    assert db.get_job(JOB)["status"] == "tailored"

    r = client.post(f"{JOB_URL}/package", json={"doc": doc, "score_after": sc["score"], "launch": False}).json()
    assert r["pdf"] and r["docx"].endswith(".docx")
    assert db.get_job(JOB)["status"] == "saved" and db.get_job(JOB)["folder"] == r["folder"]
    assert client.post(f"{JOB_URL}/applied").json() == {"ok": True}
    assert db.get_job(JOB)["status"] == "applied"
    apps = client.get("/api/applications").json()
    assert len(apps) == 1 and apps[0]["status"] == "applied"

    client.post(f"{JOB_URL}/hide", json={"hidden": True})
    assert client.get("/api/jobs").json() == []
    client.put("/api/settings", json={"filters": {"show_hidden": True}})
    assert len(client.get("/api/jobs").json()) == 1


def test_open_folder_rejects_paths_outside_applications(client, tmp_path):
    assert client.post("/api/open-folder", json={"path": str(tmp_path)}).status_code == 400
    assert client.post("/api/open", json={"what": "nonsense"}).status_code == 400


def test_search_endpoints(client):
    st = client.get("/api/search/status").json()
    assert st["running"] is False
    assert client.post("/api/search/stop").json()["running"] is False


def test_keychain_endpoints(client):
    config.ENV_FILE.write_text("WORKDAY_EMAIL=me@example.com\nWORKDAY_PASSWORD=from-env\n", encoding="utf-8")
    st = client.get("/api/account").json()
    assert st["password_in_env"] and not st["password_in_keychain"]
    r = client.post("/api/account/move-to-keychain").json()
    assert r["moved"] == ["WORKDAY_PASSWORD"] and r["account"]["password_in_keychain"]
    st = client.post("/api/account/password", json={"password": "s3cr3t-nv", "company": "NVIDIA"}).json()
    assert st["company_overrides"] == ["NVIDIA"] and "s3cr3t" not in str(st)
    assert client.post("/api/account/password", json={"password": "x", "company": "no way"}).status_code == 400
    assert "from-env" not in client.get("/api/status").text


def test_pipeline_endpoints(client):
    db.upsert_job(make_job())
    assert client.patch(f"{JOB_URL}/stage", json={"status": "applied"}).json() == {"ok": True, "status": "applied"}
    assert client.patch(f"{JOB_URL}/stage", json={"status": "hired"}).status_code == 400
    assert client.patch("/api/jobs/acme/external:nope/stage", json={"status": "applied"}).status_code == 404
    events = client.post(f"{JOB_URL}/notes", json={"note": "Recruiter called"}).json()["events"]
    assert events[-1]["kind"] == "note" and events[-1]["note"] == "Recruiter called"
    assert client.post(f"{JOB_URL}/notes", json={"note": ""}).status_code == 422
    r = client.put(f"{JOB_URL}/follow-up", json={"at": "2030-01-15", "action": "Email the recruiter"}).json()
    assert r["next_action_at"] == "2030-01-15" and r["next_action"] == "Email the recruiter"
    assert client.put(f"{JOB_URL}/follow-up", json={"at": "someday"}).status_code == 422
    assert client.put(f"{JOB_URL}/follow-up", json={"at": None}).json()["next_action_at"] is None
    board = client.get("/api/pipeline").json()
    assert [j["id"] for c in board["columns"] if c["key"] == "applied" for j in c["jobs"]] == [JOB]
    assert client.get("/api/pipeline/stats").json()["applied"] == 1
    assert [e["kind"] for e in client.get(f"{JOB_URL}/events").json()] == ["status", "note", "follow_up", "follow_up"]
    assert client.get(f"{JOB_URL}/detail").json()["events"]
    s = client.put("/api/settings", json={"keep_new_days": 30, "ghost_after_days": 14}).json()
    assert (s["keep_new_days"], s["ghost_after_days"]) == (30, 14)
    assert client.get("/api/status").json()["retention_days"] == 30
    assert client.get("/api/status").json()["db_limit"] == 50 * 1024 * 1024
    assert client.put("/api/settings", json={"max_db_mb": 300}).json()["max_db_mb"] == 300
    assert client.get("/api/status").json()["db_limit"] == 300 * 1024 * 1024
    assert client.put("/api/settings", json={"max_db_mb": 5}).status_code == 422
    assert client.put("/api/settings", json={"keep_new_days": 0}).status_code == 422


def test_restored_jobs_appear_at_startup(anon):
    import json as _json
    folder = config.APPLICATIONS / "Acme" / "Data Engineer - R7"
    folder.mkdir(parents=True)
    (folder / "application.json").write_text(_json.dumps({"job_id": "acme/external:R7", "title": "Data Engineer",
                                                          "company": "Acme", "status": "applied"}), encoding="utf-8")
    with TestClient(main.app):  # startup reads the Applications folder
        pass
    assert db.get_job("acme/external:R7")["status"] == "applied"
