import { LocalLLM } from "./llm.js";
import { refineWithModel, yamlHtml } from "./resume-parse.js";
import { changeSummary, esc, getAt, renderResume, setAt, textOf, updateBlock, valueFromText } from "./resume-view.js";
import { tailorResume } from "./tailor.js";

// ---------------------------------------------------------------- helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const enc = (id) => id.split("/").map(encodeURIComponent).join("/");
const money = (v) => (v >= 1000 ? `$${Math.round(v / 1000)}k` : `$${Math.round(v)}`);

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const { detail } = await r.json();
      // 422: FastAPI lists each invalid field as {loc, msg}.
      msg = Array.isArray(detail) ? detail.map((d) => `${(d.loc || []).slice(1).join(".") || "body"}: ${d.msg}`).join("; ")
        : detail || msg;
    } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

let toastTimer;
function toast(msg, ms = 4500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

const TYPES = ["Full-time", "Contract", "Temporary", "Part-time", "Internship"];
const STATES = { AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", PR: "Puerto Rico", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming" };

// master = master_resume.yaml data (never changed by tailoring); tailored = { resume: copy, changes: {path: rec} }
const S = {
  settings: null, status: null, jobs: [], sel: null, detail: null, master: null, masterYaml: "", tailored: null,
  view: "master", approved: [], rejected: [], tailoring: false, abort: null,
};
const llm = new LocalLLM();

// ---------------------------------------------------------------- settings
async function saveSettings(patch) {
  for (const [k, v] of Object.entries(patch)) {
    S.settings[k] = v && typeof v === "object" && !Array.isArray(v) ? { ...S.settings[k], ...v } : v;
  }
  S.settings = await api("/api/settings", { method: "PUT", body: patch });
  await loadJobs();
  if ("mandatory" in patch || "optional" in patch) watchRescore(); // keywords count toward the match score
}

// The server re-scores postings in the background after the keywords or master_resume.yaml change.
let rescoreTimer = null;
function watchRescore() {
  if (rescoreTimer) return;
  const tick = async () => {
    let st;
    try { st = await api("/api/status"); } catch { return; }
    if (st.rescoring) { $("#chip-jobs").textContent = `Re-scoring ${st.jobs} positions…`; return; }
    clearInterval(rescoreTimer);
    rescoreTimer = null;
    S.status = st;
    updateChips();
    if (st.rescore_error) toast(`Re-scoring failed: ${st.rescore_error}`, 8000);
    await loadJobs();
  };
  rescoreTimer = setInterval(tick, 1000);
  tick();
}
const saveKeywords = debounce(() => saveSettings({ mandatory: $("#mandatory").value, optional: $("#optional").value }), 700);
const saveFilter = (patch) => saveSettings({ filters: { ...S.settings.filters, ...patch } });
const saveFilterSoon = debounce(saveFilter, 500);

// ---------------------------------------------------------------- status chips
function updateChips() {
  const st = S.status || {};
  const rc = $("#chip-resume");
  rc.textContent = st.resume ? "Master: master_resume.yaml" : "No master resume";
  rc.title = st.resume ? `Built from ${st.resume.filename}. Tailoring never changes it.` : "";
  rc.className = `chip${st.master_error ? " err" : st.resume ? " ok" : ""}`;
  const pc = $("#chip-profile");
  pc.textContent = `Profile: ${st.profile || "default"}`;
  pc.title = `Your personal folder: ${st.home || ""}`;
  const last = st.last_run ? new Date(st.last_run).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "never";
  $("#chip-jobs").textContent = `${st.jobs ?? 0} jobs stored · last run ${last} · new postings kept ${st.retention_days ?? 7} days`;
}

llm.on(({ state, label, text, progress }) => {
  const c = $("#chip-model");
  const b = $("#btn-load-model");
  if (state === "loading") {
    c.className = "chip busy";
    c.textContent = `Model loading ${Math.round((progress || 0) * 100)}%`;
    c.title = text || "";
    b.disabled = true;
  } else if (state === "ready") {
    c.className = "chip ok";
    c.textContent = `Qwen2.5‑0.5B ready · ${label}`;
    c.title = `Runs locally in this tab from the bundled model files.${llm.gpuNote ? ` ${llm.gpuNote}.` : ""}`;
    b.hidden = true;
  } else if (state === "error") {
    c.className = "chip err";
    c.textContent = "Model failed to load";
    c.title = label;
    b.disabled = false;
  }
});

async function loadModel() {
  try {
    await llm.load(S.status.models, S.settings.engine === "onnx" ? "onnx" : "auto");
  } catch (e) {
    toast(`Model: ${e.message || e}`, 9000);
  }
}

// ---------------------------------------------------------------- search
function fillSearchInputs() {
  $("#mandatory").value = S.settings.mandatory || "";
  $("#optional").value = S.settings.optional || "";
}

async function runSearch() {
  await saveSettings({ mandatory: $("#mandatory").value, optional: $("#optional").value });
  if (!S.settings.mandatory.trim() && !S.settings.optional.trim()) return toast("Enter at least one keyword first.");
  try {
    await api("/api/search/run", { method: "POST", body: { full_refresh: $("#full-refresh").checked } });
    $("#full-refresh").checked = false;
    pollSearch();
  } catch (e) { toast(e.message); }
}

let pollTimer = null;
function pollSearch() {
  if (pollTimer) return;
  let ticks = 0;
  const tick = async () => {
    let st;
    try { st = await api("/api/search/status"); } catch { return; }
    renderProgress(st);
    if (++ticks % 4 === 0) await loadJobs();
    if (!st.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      S.status = await api("/api/status");
      updateChips();
      await loadJobs();
      const errs = st.errors.length ? ` · ${st.errors.length} error(s), see Log` : "";
      toast(`Search finished: ${st.new_jobs} new, ${st.refreshed} refreshed${errs}`);
    }
  };
  pollTimer = setInterval(tick, 1200);
  tick();
}

function renderProgress(st) {
  $("#progress").hidden = false;
  $("#btn-run").disabled = st.running;
  $("#btn-stop").hidden = !st.running;
  const pct = st.total ? Math.round((st.done / st.total) * 100) : st.running ? 3 : 100;
  $("#progress-bar").style.width = `${pct}%`;
  const now = st.current?.length ? ` · now: ${st.current.join(", ")}` : "";
  $("#progress-text").textContent = st.running
    ? `${st.mode} · ${st.done}/${st.total} companies · ${st.scanned} in window · ${st.new_jobs} new${now}`
    : `Last ${st.mode || "run"}: ${st.new_jobs} new, ${st.refreshed} refreshed, ${st.rejected} filtered out` + (st.errors.length ? `, ${st.errors.length} errors` : "");
  $("#log").textContent = [...st.log, ...st.errors.map((e) => `ERROR ${e}`)].join("\n");
}

// ---------------------------------------------------------------- filters
function renderFilters() {
  const f = S.settings.filters;
  $("#f-city").value = f.city || "";
  $("#f-remote").checked = !!f.remote_only;
  $("#f-salary").value = f.min_salary || "";
  $("#f-nosalary").checked = f.include_no_salary !== false;
  $("#f-reqopt").checked = !!f.require_optional;
  $("#f-hidden").checked = !!f.show_hidden;
  const on = new Set(f.types || []);
  $("#f-types").innerHTML = '<span class="flabel">Types</span>' + TYPES.map((t) =>
    `<button class="type-toggle t-${t}${on.has(t) ? "" : " off"}" data-type="${t}" aria-pressed="${on.has(t)}" title="Click to ${on.has(t) ? "omit" : "include"} ${t} roles">${t}</button>`).join("");
  const sel = new Set(f.states || []);
  $("#btn-states").textContent = sel.size ? `${sel.size} state${sel.size > 1 ? "s" : ""}: ${[...sel].slice(0, 4).join(", ")}${sel.size > 4 ? "…" : ""}` : "All states";
  $("#states-pop").innerHTML = `<div class="states-grid">${Object.entries(STATES).map(([c, n]) =>
    `<label title="${n}"><input type="checkbox" value="${c}"${sel.has(c) ? " checked" : ""}>${c}</label>`).join("")}</div>
    <p class="hint">Remote roles with no listed state are kept when you pick states.</p>
    <div class="pop-actions"><button class="btn small" data-act="clear">Clear</button><button class="btn small primary" data-act="done">Done</button></div>`;
}

// ---------------------------------------------------------------- job list
async function loadJobs() {
  S.jobs = await api("/api/jobs");
  renderJobs();
}

function daysAgo(iso) {
  const d = Math.round((Date.now() - new Date(`${iso}T12:00:00`).getTime()) / 86400000);
  return d <= 0 ? "today" : d === 1 ? "1 day ago" : `${d} days ago`;
}

function renderJobs() {
  $("#list-count").textContent = `${S.jobs.length} position${S.jobs.length === 1 ? "" : "s"}`;
  if (!S.jobs.length) {
    $("#job-list").innerHTML = `<div class="empty">${S.status?.jobs ? "No positions match the current filters." : "Upload your master resume, enter keywords, then run a job search."}</div>`;
    return;
  }
  $("#job-list").innerHTML = S.jobs.map((j) => {
    const sc = j.match_score;
    const cls = sc == null ? "lo" : sc >= 45 ? "hi" : sc >= 25 ? "mid" : "lo";
    const where = j.states.length ? j.states.join(", ") : j.location;
    const badges = [`<span class="badge t-${esc(j.employment_type)}">${esc(j.employment_type)}</span>`];
    if (j.remote_type === "Remote" || j.remote_type === "Hybrid") badges.push(`<span class="badge b-remote">${j.remote_type}</span>`);
    if (j.salary_max) badges.push(`<span class="badge b-salary">${money(j.salary_min)}–${money(j.salary_max)}</span>`);
    j.optional_hits.forEach((k) => badges.push(`<span class="badge b-opt">${esc(k)}</span>`));
    if (j.status && j.status !== "new") badges.push(`<span class="badge b-status">${esc(j.status)}</span>`);
    return `<div class="job${j.id === S.sel ? " active" : ""}${j.hidden ? " is-hidden" : ""}" data-id="${esc(j.id)}" tabindex="0">
      <div class="score ${cls}" title="Match with your master resume">${sc ?? "–"}</div>
      <div class="job-title">${esc(j.title)}</div>
      <div class="job-sub">${esc(j.company)} · ${esc(where)} · ${daysAgo(j.posted_date)}</div>
      <div class="badges">${badges.join("")}</div>
      <button class="hide-btn" data-hide="${esc(j.id)}" title="${j.hidden ? "Unhide" : "Hide this position"}" aria-label="Hide">${j.hidden ? "↺" : "✕"}</button>
    </div>`;
  }).join("");
}

// ---------------------------------------------------------------- job detail + JD
async function selectJob(id) {
  if (S.tailoring) return toast("Tailoring in progress — stop it first.");
  S.sel = id;
  $$(".job").forEach((el) => el.classList.toggle("active", el.dataset.id === id));
  try {
    S.detail = await api(`/api/jobs/${enc(id)}/detail`);
  } catch (e) { return toast(e.message); }
  const t = S.detail.tailored;
  S.tailored = t ? t.doc : null;
  S.approved = t ? t.approved : [];
  S.rejected = t ? t.rejected : [];
  S.view = S.tailored ? "tailored" : "master";
  renderJD();
  renderResumePane();
}

function sanitize(html) {
  const doc = new DOMParser().parseFromString(html || "", "text/html");
  doc.querySelectorAll("script,style,iframe,object,embed,link,meta,form,img,svg,video,audio").forEach((n) => n.remove());
  doc.querySelectorAll("*").forEach((el) => {
    for (const a of [...el.attributes]) {
      if (!(el.tagName === "A" && a.name === "href" && /^https?:/i.test(a.value))) el.removeAttribute(a.name);
    }
    if (el.tagName === "A") { el.target = "_blank"; el.rel = "noopener"; }
  });
  return doc.body;
}

function highlightJD(root, d) {
  const a = d.analysis;
  const terms = new Map();
  const add = (alias, cls) => { const k = alias.toLowerCase(); if (alias.length > 1 && !terms.has(k)) terms.set(k, cls); };
  const split = (s) => (s || "").split(/[,;\n]/).map((x) => x.trim()).filter(Boolean);
  split(S.settings.mandatory).forEach((k) => add(k, "hl-must"));
  a.missing.forEach((m) => (a.aliases[m.keyword] || [m.keyword]).forEach((x) => add(x, "hl-miss")));
  a.matched.forEach((m) => (a.aliases[m] || [m]).forEach((x) => add(x, "hl-hit")));
  split(S.settings.optional).forEach((k) => add(k, "hl-opt"));
  if (!terms.size) return;
  const alts = [...terms.keys()].sort((x, y) => y.length - x.length).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(?<![A-Za-z0-9])(${alts.join("|")})(?![A-Za-z0-9+#])`, "gi");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.nodeValue;
    re.lastIndex = 0;
    if (!re.test(text)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(text))) {
      frag.append(text.slice(last, m.index));
      const span = document.createElement("span");
      span.className = terms.get(m[0].toLowerCase()) || "hl-hit";
      span.textContent = m[0];
      frag.append(span);
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    node.replaceWith(frag);
  }
}

function renderJD() {
  const d = S.detail;
  const a = d.analysis;
  const salary = d.salary_max ? `${money(d.salary_min)} – ${money(d.salary_max)} / yr` : "not listed";
  const link = $("#jd-link");
  link.href = d.url;
  link.hidden = false;
  const chips = [...a.matched.map((k) => `<span class="kw hit">${esc(k)}</span>`), ...a.missing.map((m) => `<span class="kw miss" title="${esc(m.context)}">${esc(m.keyword)}</span>`)].join("");
  $("#jd-view").innerHTML = `
    <h1>${esc(d.title)}</h1>
    <dl class="meta">
      <dt>Company</dt><dd>${esc(d.company)}</dd>
      <dt>Location</dt><dd>${esc((d.locations || []).join(" · ") || d.location)}</dd>
      <dt>Job type</dt><dd><span class="badge t-${esc(d.employment_type)}">${esc(d.employment_type)}</span> ${d.worker_sub_type ? `<span class="hint">${esc(d.worker_sub_type)}</span>` : ""}</dd>
      <dt>Work mode</dt><dd>${esc(d.remote_type)}${d.remote_raw ? ` <span class="hint">(${esc(d.remote_raw)})</span>` : ""}</dd>
      <dt>Salary</dt><dd>${salary}</dd>
      <dt>Posted</dt><dd>${esc(d.posted_date)} (${daysAgo(d.posted_date)})</dd>
      <dt>Req ID</dt><dd>${esc(d.req_id)}</dd>
    </dl>
    <div class="legend"><span class="l-hit">in your resume</span><span class="l-miss">missing</span><span class="l-opt">optional keyword</span></div>
    <div class="kw-chips">${chips || '<span class="hint">No ATS keywords detected.</span>'}</div>
    <div class="jd-body" id="jd-body"></div>`;
  const body = sanitize(d.description_html);
  const holder = $("#jd-body");
  holder.append(...body.childNodes);
  highlightJD(holder, d);
}

// ---------------------------------------------------------------- resume pane
const viewOpts = () => ({ diff: true, changes: S.tailored?.changes, editable: !S.tailoring, approved: S.approved });

function renderLegend() {
  const el = $("#change-legend");
  const on = S.view === "tailored" && S.tailored && !S.tailoring;
  el.hidden = !on;
  if (!on) return;
  const c = changeSummary(S.tailored.changes);
  const parts = [`<span class="lg-chg">${c.applied} change${c.applied === 1 ? "" : "s"}</span>`];
  if (c.check) parts.push(`<span class="lg-warn">${c.check} to check</span>`);
  if (c.held) parts.push(`<span class="lg-held">${c.held} AI version${c.held === 1 ? "" : "s"} held back</span>`);
  if (c.undone) parts.push(`<span class="lg-undone">${c.undone} undone</span>`);
  el.innerHTML = `${parts.join("")}<span class="hint">Hover a changed line for <b>Undo</b> at its end · click a line to edit it</span>`;
}

function renderResumePane() {
  const d = S.detail;
  $("#tab-tailored").disabled = !S.tailored;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === S.view));
  const view = $("#resume-view");
  if (S.view === "tailored" && S.tailored) {
    renderResume(view, S.tailored.resume, viewOpts());
  } else {
    renderResume(view, S.master);
  }
  renderLegend();
  $("#btn-edit-yaml").hidden = !(S.view === "master" && S.master);
  const line = $("#score-line");
  if (d) {
    const a = d.analysis;
    const before = d.match_score ?? a.score;
    const after = d.tailored?.score_after;
    line.title = `Keyword coverage ${a.coverage}% · wording similarity ${a.similarity}% · title alignment ${a.title_alignment}%`;
    const delta = after - before;
    line.innerHTML = `Match <b>${before ?? "–"}</b>` + (after != null ? ` → <b>${after}</b> <span class="${delta >= 0 ? "up" : "down"}">${delta >= 0 ? "+" : ""}${delta}</span>` : "");
  } else line.innerHTML = "";
  const ready = !!(d && S.master);
  $("#btn-tailor").disabled = !ready || S.tailoring;
  $("#btn-tailor").textContent = S.tailored ? "Re-tailor for this position" : "Tailor my resume for this position only";
  $("#btn-tailor-stop").hidden = !S.tailoring;
  $("#btn-package").disabled = !S.tailored || S.tailoring;
  $("#btn-apply").disabled = !S.tailored || S.tailoring;
  $("#btn-applied").disabled = !d || !d.folder || d.status === "applied";
}

const persistEdits = debounce(async () => {
  if (!S.detail || !S.tailored) return;
  const doc = S.tailored;
  const sc = await api(`/api/jobs/${enc(S.detail.id)}/score`, { method: "POST", body: { resume: doc.resume } });
  await api(`/api/jobs/${enc(S.detail.id)}/tailored`, { method: "PUT", body: { doc, score_after: sc.score, approved: S.approved, rejected: S.rejected } });
  S.detail.tailored = { ...(S.detail.tailored || {}), doc, score_after: sc.score };
  const keep = document.activeElement;
  if (!(keep && keep.isContentEditable)) renderResumePane();
  else $("#score-line").innerHTML = `Match <b>${S.detail.match_score}</b> → <b>${sc.score}</b>`;
}, 900);

/** Undo puts your master wording back; Redo / "Use AI version" puts the tailored wording in. */
function toggleChange(path) {
  const rec = S.tailored?.changes[path];
  if (!rec) return;
  const toOrig = rec.state === "alt";
  rec.state = toOrig ? "orig" : "alt";
  setAt(S.tailored.resume, path, structuredClone(toOrig ? rec.orig : rec.alt));
  if (!toOrig && rec.held) { rec.warn = `you chose the AI version, which ${rec.held.replace(/^adds/, "added").replace(/^uses/, "used")}`; delete rec.held; }
  if (!updateBlock($("#resume-view"), path, S.tailored.resume, viewOpts())) renderResumePane();
  renderLegend();
  persistEdits();
}

/** You typed in a line of the tailored resume. */
function onLineEdited(path, text) {
  const doc = S.tailored;
  const value = valueFromText(path, text);
  let rec = doc.changes[path];
  if (!rec) rec = doc.changes[path] = { orig: structuredClone(getAt(doc.resume, path)), alt: value, state: "alt" };
  setAt(doc.resume, path, value);
  if (textOf(value) === textOf(rec.orig)) delete doc.changes[path];
  else Object.assign(rec, { alt: value, state: "alt", edited: true, held: undefined, warn: undefined });
  persistEdits();
}

// ---------------------------------------------------------------- keyword approval
function askKeywords(missing) {
  const dlg = $("#kw-dialog");
  const items = missing.map((m) => ({ ...m, decision: S.approved.includes(m.keyword) ? "approve" : S.rejected.includes(m.keyword) ? "reject" : null }));
  if (!items.length) return Promise.resolve({ approved: [], rejected: [] });
  let i = Math.max(0, items.findIndex((x) => !x.decision));
  return new Promise((resolve) => {
    const finish = (val) => { dlg.close(); document.removeEventListener("keydown", onKey); resolve(val); };
    const render = () => {
      const it = items[i];
      const now = it.decision ? `<span class="kw ${it.decision === "approve" ? "hit" : "miss"}">${it.decision === "approve" ? "approved" : "rejected"}</span>` : "";
      $("#kw-step").innerHTML = `
        <div class="hint">Keyword ${i + 1} of ${items.length} · appears ${it.count}× in the job description ${now}</div>
        <div class="kw-name">${esc(it.keyword)}</div>
        <div class="kw-ctx">${esc(it.context || "")}</div>
        <div class="kw-buttons">
          <button type="button" class="btn reject" data-d="reject">✕ Reject <span class="hint">(R)</span></button>
          <button type="button" class="btn approve" data-d="approve">✓ Approve — I have this <span class="hint">(A)</span></button>
        </div>
        <div class="kw-nav">
          <button type="button" class="linkish" data-nav="-1"${i === 0 ? " disabled" : ""}>← Back</button>
          <button type="button" class="linkish" data-nav="1"${i === items.length - 1 ? " disabled" : ""}>Next →</button>
        </div>`;
      $("#kw-summary").innerHTML = items.map((x, k) =>
        `<span class="kw ${x.decision === "approve" ? "hit" : x.decision === "reject" ? "miss" : "pending"}" data-k="${k}" title="Click to revisit">${esc(x.keyword)}</span>`).join("");
      $("#kw-start").disabled = items.some((x) => !x.decision);
    };
    const go = (k) => { i = Math.min(items.length - 1, Math.max(0, k)); render(); };
    const decide = (d) => {
      items[i].decision = d;
      // Next undecided keyword after this one; otherwise simply step forward (re-tailoring keeps old decisions).
      const next = items.findIndex((x, k) => k > i && !x.decision);
      const any = items.findIndex((x) => !x.decision);
      go(next >= 0 ? next : i + 1 < items.length ? i + 1 : any >= 0 ? any : i);
    };
    const onKey = (e) => {
      if (e.key === "a" || e.key === "A") decide("approve");
      else if (e.key === "r" || e.key === "R") decide("reject");
      else if (e.key === "ArrowLeft") go(i - 1);
      else if (e.key === "ArrowRight") go(i + 1);
    };
    $("#kw-step").onclick = (e) => {
      const b = e.target.closest("[data-d]");
      if (b) return decide(b.dataset.d);
      const n = e.target.closest("[data-nav]");
      if (n) go(i + Number(n.dataset.nav));
    };
    $("#kw-summary").onclick = (e) => { const c = e.target.closest("[data-k]"); if (c) { i = +c.dataset.k; render(); } };
    $("#kw-approve-rest").onclick = () => { items.forEach((x) => { if (!x.decision) x.decision = "approve"; }); render(); };
    $("#kw-reject-rest").onclick = () => { items.forEach((x) => { if (!x.decision) x.decision = "reject"; }); render(); };
    $("#kw-cancel").onclick = () => finish(null);
    dlg.oncancel = (e) => { e.preventDefault(); finish(null); };
    $("#kw-start").onclick = () => finish({
      approved: items.filter((x) => x.decision === "approve").map((x) => x.keyword),
      rejected: items.filter((x) => x.decision === "reject").map((x) => x.keyword),
    });
    document.addEventListener("keydown", onKey);
    render();
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- tailoring
function setTailorStatus(text, busy = true) {
  const el = $("#tailor-status");
  el.hidden = !text;
  el.innerHTML = text ? `${busy ? '<span class="dot"></span>' : ""}<span>${esc(text)}</span><span class="spacer"></span><span id="tok-rate"></span>` : "";
}

async function onTailor() {
  if (!S.detail || !S.master || S.tailoring) return;
  if (llm.state !== "ready") {
    setTailorStatus("Loading Qwen2.5‑0.5B from the bundled model files…");
    await loadModel();
    if (llm.state !== "ready") return setTailorStatus("");
  }
  const decision = await askKeywords(S.detail.analysis.missing);
  if (!decision) return setTailorStatus("");
  S.approved = decision.approved;
  S.rejected = decision.rejected;
  const job = S.detail;
  S.tailoring = true;
  S.abort = new AbortController();
  S.view = "tailored";
  const view = $("#resume-view");
  let result;
  try {
    result = await tailorResume({
      llm, master: S.master, job, analysis: job.analysis, approved: S.approved,
      maxBullets: Number(S.settings.max_bullets ?? 12), signal: S.abort.signal,
      ui: {
        onRender: (res, changes) => { S.tailored = { resume: res, changes }; renderResumePane(); },
        onBlock: (path) => { if (!updateBlock(view, path, S.tailored.resume, viewOpts())) renderResumePane(); },
        onStatus: (t) => setTailorStatus(llm.backend === "onnx" ? `${t} (CPU model: the page pauses a few seconds as each line starts)` : t),
        onTokens: (st) => {
          const r = $("#tok-rate");
          if (r) r.textContent = `${st.tokens} tokens · ${(st.tokens / ((performance.now() - st.started) / 1000)).toFixed(1)} tok/s`;
        },
      },
    });
  } catch (e) {
    S.tailoring = false;
    setTailorStatus("");
    renderResumePane();
    return toast(`Tailoring failed: ${e.message || e}`, 8000);
  }
  S.tailoring = false;
  const doc = { resume: result.res, changes: result.changes };
  S.tailored = doc;
  const sc = await api(`/api/jobs/${enc(job.id)}/score`, { method: "POST", body: { resume: doc.resume } });
  await api(`/api/jobs/${enc(job.id)}/tailored`, { method: "PUT", body: { doc, score_after: sc.score, approved: S.approved, rejected: S.rejected } });
  job.tailored = { doc, score_after: sc.score, approved: S.approved, rejected: S.rejected };
  if (job.status === "new" || !job.status) job.status = "tailored";
  const st = result.stats;
  const extra = [st.flagged ? `${st.flagged} marked amber to check` : "", st.held ? `${st.held} AI version(s) held back because they added facts you didn't approve` : ""].filter(Boolean).join(", ");
  setTailorStatus(`Done in ${st.seconds.toFixed(0)}s — rewrote ${st.rewritten} line(s)${extra ? ` (${extra})` : ""}. Hover any changed line to undo it.`, false);
  renderResumePane();
  await loadJobs();
  toast(`Match ${job.match_score} → ${sc.score}. Review the green edits before applying.`);
}

// ---------------------------------------------------------------- package / apply
async function onPackage(launch) {
  if (!S.detail || !S.tailored) return;
  const btn = launch ? $("#btn-apply") : $("#btn-package");
  btn.disabled = true;
  try {
    const r = await api(`/api/jobs/${enc(S.detail.id)}/package`, {
      method: "POST",
      body: { doc: S.tailored, score_after: S.detail.tailored?.score_after, approved: S.approved, rejected: S.rejected, launch },
    });
    let msg = `Saved to ${r.folder}`;
    if (r.pdf_error) msg += ` (PDF skipped: ${r.pdf_error})`;
    if (launch) msg = r.launched
      ? "Opened the posting in a separate browser window. Sign in if asked; standard fields fill automatically. Review, then click Submit yourself."
      : `Saved the package, but the browser could not open: ${r.launch_error}`;
    toast(msg, 10000);
    S.detail = await api(`/api/jobs/${enc(S.detail.id)}/detail`);
    renderResumePane();
    await loadJobs();
  } catch (e) {
    toast(e.message, 8000);
  } finally {
    renderResumePane();
  }
}

async function onMarkApplied() {
  if (!S.detail) return;
  await api(`/api/jobs/${enc(S.detail.id)}/applied`, { method: "POST" });
  S.detail.status = "applied";
  toast("Marked as applied. application.json in the job's folder was updated.");
  renderResumePane();
  await loadJobs();
}

// ---------------------------------------------------------------- master resume: upload -> model -> YAML
function yamlDialog(mode) {
  const editing = mode === "edit";
  $("#yaml-title").textContent = editing ? "master_resume.yaml" : "Building master_resume.yaml";
  $("#yaml-live").hidden = editing;
  $("#yaml-text").hidden = !editing;
  $("#yaml-save").hidden = !editing;
  $("#yaml-skip").hidden = editing;
  $("#yaml-open").hidden = !editing;
  $("#yaml-status").textContent = "";
  const dlg = $("#yaml-dialog");
  if (!dlg.open) dlg.showModal();
  return dlg;
}

async function onResumeFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  let parsed;
  try { parsed = await api("/api/resume/parse", { method: "POST", body: fd }); } catch (e) { return toast(e.message, 8000); }
  const { draft, filename } = parsed;
  const dlg = yamlDialog("build");
  const live = $("#yaml-live"), status = $("#yaml-status");
  $("#yaml-intro").innerHTML = `The local model reads the top of <b>${esc(filename)}</b> and each job and education heading, and splits them into fields. Bullets and your summary are copied word for word, and every field it fills must appear in your resume text, so it can't invent anything.`;
  const abort = new AbortController();
  const show = (task) => {
    live.innerHTML = yamlHtml(draft, task?.target, task?.live);
    live.querySelector(".hot")?.scrollIntoView({ block: "nearest" });
  };
  show(null);
  $("#yaml-skip").onclick = () => { abort.abort(); llm.interrupt(); };
  $("#yaml-cancel").onclick = () => { abort.abort(); llm.interrupt(); dlg.close(); };
  dlg.oncancel = (e) => { e.preventDefault(); $("#yaml-cancel").click(); };

  let how = "quick rule-based parse, no model";
  const started = performance.now();
  if (llm.state !== "ready") {
    status.innerHTML = '<span class="dot"></span>Loading Qwen2.5‑0.5B from the bundled model files…';
    await loadModel();
  }
  if (!dlg.open) return;
  if (llm.state === "ready" && !abort.signal.aborted) {
    const r = await refineWithModel({
      llm, draft, signal: abort.signal, ui: {
        onStatus: (t) => { status.innerHTML = `<span class="dot"></span>${esc(t)} <span class="hint" id="yaml-rate"></span>`; },
        onTokens: (n) => { const el = $("#yaml-rate"); if (el) el.textContent = `${n} tokens · ${llm.label}`; },
        onUpdate: show,
      },
    });
    if (!dlg.open) return;
    if (r.done) how = `fields split by Qwen2.5-0.5B, ${llm.label}`;
    status.textContent = abort.signal.aborted
      ? "Stopped. The rest uses the quick rule-based parse."
      : `Done in ${Math.round((performance.now() - started) / 1000)}s. The model filled ${r.used} field(s) from ${r.done} part(s) of your resume.`;
  } else {
    status.textContent = llm.state === "ready" ? "Skipped the model: this is the quick rule-based parse."
      : `The model is unavailable (${llm.label || "not loaded"}), so this is the quick rule-based parse.`;
  }
  let preview;
  try {
    preview = await api("/api/resume/preview", { method: "POST", body: { data: draft, filename, how } });
  } catch (e) { status.textContent = e.message; return; }
  if (!dlg.open) return;
  const summary = status.textContent;
  yamlDialog("edit");
  $("#yaml-title").textContent = "Review master_resume.yaml";
  $("#yaml-open").hidden = true;
  $("#yaml-text").value = preview.yaml;
  status.textContent = `${summary} Check it, edit anything below, then save. Tailoring always works on a copy of this file.`;
  $("#yaml-save").onclick = () => saveMasterYaml($("#yaml-text").value, filename, true);
}

async function openYamlEditor() {
  let r;
  try { r = await api("/api/resume"); } catch (e) { return toast(e.message); }
  yamlDialog("edit");
  $("#yaml-intro").innerHTML = `Your master resume as data: <code>${esc(`${S.status.home}\\master_resume.yaml`)}</code>. Tailoring never changes this file; only your own edits do.`;
  $("#yaml-text").value = r.yaml;
  if (r.error) $("#yaml-status").textContent = r.error;
  $("#yaml-save").onclick = () => saveMasterYaml($("#yaml-text").value, null, false);
  $("#yaml-cancel").onclick = () => $("#yaml-dialog").close();
  $("#yaml-dialog").oncancel = null;
}

async function saveMasterYaml(yamlText, filename, newUpload) {
  $("#yaml-save").disabled = true;
  $("#yaml-status").textContent = "Saving…";
  try {
    const r = await api("/api/resume", { method: "PUT", body: { yaml: yamlText, filename, new_upload: newUpload } });
    S.master = r.data;
    S.masterYaml = r.yaml;
    $("#yaml-dialog").close();
    S.status = await api("/api/status");
    updateChips();
    await loadJobs();
    if (S.sel) await selectJob(S.sel);
    else { S.view = "master"; renderResumePane(); }
    toast(`${newUpload ? "Saved" : "Updated"} master_resume.yaml. Re-scoring ${r.jobs} position(s)…`);
    watchRescore();
  } catch (e) {
    $("#yaml-status").textContent = e.message;
  } finally {
    $("#yaml-save").disabled = false;
  }
}

// ---------------------------------------------------------------- settings dialog
const PROFILE_FIELDS = [
  ["first_name", "First name"], ["last_name", "Last name"], ["email", "Email"], ["phone", "Phone"],
  ["phone_type", "Phone type", ["Mobile", "Home", "Work"]], ["address1", "Address line 1"], ["address2", "Address line 2"],
  ["city", "City"], ["state", "State", ["", ...Object.keys(STATES)]], ["postal_code", "ZIP code"],
  ["country", "Country"], ["linkedin", "LinkedIn URL"], ["website", "Website / portfolio"], ["github", "GitHub URL"],
  ["authorized_us", "Authorized to work in the US?", ["Yes", "No"]], ["needs_sponsorship", "Need visa sponsorship?", ["No", "Yes"]],
  ["previously_employed", "Worked at the company before?", ["No", "Yes"]], ["how_heard", "How did you hear about us?"],
];

function renderAccount() {
  const a = S.status.account || {};
  const ok = (v) => `<span class="${v ? "good" : "bad"}">${v ? "set" : "not set"}</span>`;
  $("#home-path").textContent = S.status.home || "";
  $("#env-path").textContent = a.path || "";
  $("#env-status").innerHTML = `WORKDAY_EMAIL: ${a.email ? `<b>${esc(a.email)}</b>` : ok(false)} · WORKDAY_PASSWORD: ${ok(a.password_set)}`
    + (a.company_overrides?.length ? ` · company-specific: ${esc(a.company_overrides.join(", "))}` : "");
}

async function openSettings() {
  const s = S.settings;
  const form = $("#settings-form");
  S.status = await api("/api/status");
  renderAccount();
  form.current_employer.value = s.current_employer || "";
  form.engine.value = s.engine === "onnx" ? "onnx" : "auto";
  form.upload_format.value = s.upload_format || "docx";
  form.max_bullets.value = s.max_bullets ?? 12;
  $("#profile-fields").innerHTML = PROFILE_FIELDS.map(([k, label, opts]) => {
    const v = s.profile[k] ?? "";
    const input = opts
      ? `<select name="p_${k}">${opts.map((o) => `<option value="${esc(o)}"${o === v ? " selected" : ""}>${esc(o || "—")}</option>`).join("")}</select>`
      : `<input name="p_${k}" value="${esc(v)}" autocomplete="off">`;
    return `<label>${esc(label)}${input}</label>`;
  }).join("");
  await renderCompanies();
  $("#settings-dialog").showModal();
}

async function renderCompanies() {
  const list = await api("/api/companies");
  const ok = list.filter((c) => !c.error);
  const yours = ok.filter((c) => c.source === "yours").length;
  $("#companies-count").textContent = `${ok.filter((c) => c.active).length} of ${ok.length} searched · ${ok.length - yours} shared, ${yours} yours`;
  $("#companies").innerHTML = list.map((c) => c.error
    ? `<div class="err" title="${esc(c.url)}">${esc(c.name || c.url)}: ${esc(c.error)}</div>`
    : `<label title="${esc(c.url)}${c.enabled ? "" : " (enabled: false in the file)"}"><input type="checkbox" data-key="${esc(c.key)}"${c.active ? " checked" : ""}${c.enabled ? "" : " disabled"}> ${esc(c.name)}${c.source === "yours" ? ' <span class="hint">(yours)</span>' : ""}</label>`).join("")
    || '<span class="hint">No companies yet: add Workday sites to companies.yaml, or use the Add box.</span>';
}

async function saveSettingsDialog() {
  const form = $("#settings-form");
  const profile = {};
  PROFILE_FIELDS.forEach(([k]) => { profile[k] = form[`p_${k}`].value.trim(); });
  const disabled = $$("#companies input[type=checkbox]").filter((c) => !c.checked).map((c) => c.dataset.key);
  const engineChanged = form.engine.value !== S.settings.engine;
  await saveSettings({
    current_employer: form.current_employer.value.trim(), engine: form.engine.value,
    upload_format: form.upload_format.value, max_bullets: Number(form.max_bullets.value || 12),
    profile, disabled_companies: disabled,
  });
  $("#settings-dialog").close();
  toast(engineChanged && llm.state === "ready" ? "Saved. Reload the page to switch AI engine." : "Settings saved.");
}

// ---------------------------------------------------------------- applications dialog
async function openApplications() {
  const apps = await api("/api/applications");
  $("#apps-list").innerHTML = apps.length ? apps.map((a) => `
    <div class="app-row">
      <div><strong>${esc(a.company)}</strong> — ${esc(a.title)}
        <div class="sub">${esc(a.status)} · prepared ${esc((a.prepared_at || "").replace("T", " "))}${a.applied_at ? ` · applied ${esc(a.applied_at.replace("T", " "))}` : ""} · match ${a.match_score_before ?? "–"} → ${a.match_score_after ?? "–"}</div>
      </div>
      <div><button class="btn small" data-open="${esc(a.folder)}">Open folder</button> <a class="btn small" href="${esc(a.url)}" target="_blank" rel="noopener">Posting ↗</a></div>
    </div>`).join("") : '<p class="hint">Nothing saved yet. Use “Save package” or “Apply with autofill” on a tailored resume.</p>';
  $("#apps-dialog").showModal();
}

// ---------------------------------------------------------------- events
function bindEvents() {
  $("#btn-run").onclick = runSearch;
  $("#btn-stop").onclick = () => api("/api/search/stop", { method: "POST" });
  $("#btn-log").onclick = () => { $("#log").hidden = !$("#log").hidden; };
  $("#mandatory").addEventListener("input", saveKeywords);
  $("#optional").addEventListener("input", saveKeywords);
  [$("#mandatory"), $("#optional")].forEach((el) => el.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); }));
  $("#btn-load-model").onclick = loadModel;

  $("#resume-file").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (S.tailoring) return toast("Tailoring in progress — stop it first.");
    onResumeFile(file);
  };
  $("#btn-edit-yaml").onclick = openYamlEditor;
  $("#yaml-open").onclick = () => api("/api/open", { method: "POST", body: { what: "master" } }).catch((err) => toast(err.message));

  // filters
  $("#f-city").addEventListener("input", () => saveFilterSoon({ city: $("#f-city").value }));
  $("#f-salary").addEventListener("input", () => saveFilterSoon({ min_salary: Number($("#f-salary").value || 0) }));
  $("#f-remote").onchange = () => saveFilter({ remote_only: $("#f-remote").checked });
  $("#f-nosalary").onchange = () => saveFilter({ include_no_salary: $("#f-nosalary").checked });
  $("#f-reqopt").onchange = () => saveFilter({ require_optional: $("#f-reqopt").checked });
  $("#f-hidden").onchange = () => saveFilter({ show_hidden: $("#f-hidden").checked });
  $("#f-types").onclick = async (e) => {
    const b = e.target.closest("[data-type]");
    if (!b) return;
    const on = new Set(S.settings.filters.types);
    on.has(b.dataset.type) ? on.delete(b.dataset.type) : on.add(b.dataset.type);
    await saveFilter({ types: TYPES.filter((t) => on.has(t)) });
    renderFilters();
  };
  $("#btn-states").onclick = () => {
    const pop = $("#states-pop");
    pop.hidden = !pop.hidden;
    $("#btn-states").setAttribute("aria-expanded", String(!pop.hidden));
  };
  $("#states-pop").onclick = async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "clear") $$("#states-pop input").forEach((c) => (c.checked = false));
    if (act) {
      $("#states-pop").hidden = true;
      await saveFilter({ states: $$("#states-pop input:checked").map((c) => c.value) });
      renderFilters();
    }
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".states") && !$("#states-pop").hidden) $("#states-pop").querySelector('[data-act="done"]').click();
  });

  // job list
  $("#job-list").onclick = async (e) => {
    const hide = e.target.closest("[data-hide]");
    if (hide) {
      e.stopPropagation();
      const j = S.jobs.find((x) => x.id === hide.dataset.hide);
      await api(`/api/jobs/${enc(hide.dataset.hide)}/hide`, { method: "POST", body: { hidden: !j?.hidden } });
      return loadJobs();
    }
    const card = e.target.closest(".job");
    if (card) selectJob(card.dataset.id);
  };
  $("#job-list").onkeydown = (e) => { if (e.key === "Enter" && e.target.classList.contains("job")) selectJob(e.target.dataset.id); };

  // resume pane
  $$(".tab").forEach((t) => (t.onclick = () => { if (!t.disabled) { S.view = t.dataset.view; renderResumePane(); } }));
  $("#btn-tailor").onclick = onTailor;
  $("#btn-tailor-stop").onclick = () => { S.abort?.abort(); llm.interrupt(); };
  $("#btn-package").onclick = () => onPackage(false);
  $("#btn-apply").onclick = () => onPackage(true);
  $("#btn-applied").onclick = onMarkApplied;
  const rv = $("#resume-view");
  // Keep focus where it is so the line isn't repainted under the pointer before the click lands.
  rv.addEventListener("mousedown", (e) => { if (e.target.closest(".chg-ctl")) e.preventDefault(); });
  rv.addEventListener("click", (e) => {
    const b = e.target.closest(".chg-ctl");
    if (!b || S.view !== "tailored" || !S.tailored || S.tailoring) return;
    e.preventDefault();
    toggleChange(b.closest("[data-row]").dataset.row);
  });
  rv.addEventListener("input", (e) => {
    const el = e.target.closest("[data-path]");
    if (!el || S.view !== "tailored" || !S.tailored || S.tailoring) return;
    onLineEdited(el.dataset.path, el.innerText.replace(/\s+/g, " ").trim());
  });
  rv.addEventListener("focusout", (e) => {
    const el = e.target.closest?.("[data-path]");
    if (!el || S.view !== "tailored" || !S.tailored || S.tailoring) return;
    updateBlock(rv, el.dataset.path, S.tailored.resume, viewOpts());
    renderLegend();
  });

  // dialogs
  $("#btn-settings").onclick = openSettings;
  $("#settings-cancel").onclick = () => $("#settings-dialog").close();
  $("#settings-save").onclick = saveSettingsDialog;
  const tickAll = (on) => $$("#companies input[type=checkbox]:not(:disabled)").forEach((c) => { c.checked = on; });
  $("#co-all").onclick = () => tickAll(true);
  $("#co-none").onclick = () => tickAll(false);
  $("#btn-add-co").onclick = async () => {
    try {
      await api("/api/companies", { method: "POST", body: { name: $("#new-co-name").value, url: $("#new-co-url").value } });
      $("#new-co-name").value = $("#new-co-url").value = "";
      await renderCompanies();
      toast("Added to my_companies.yaml in your personal folder");
    } catch (e) { toast(e.message, 7000); }
  };
  $("#btn-open-home").onclick = () => api("/api/open", { method: "POST", body: { what: "home" } }).catch((err) => toast(err.message));
  $("#btn-open-env").onclick = () => api("/api/open", { method: "POST", body: { what: "env" } }).catch((err) => toast(err.message));
  $("#btn-env-refresh").onclick = async () => { S.status = await api("/api/status"); renderAccount(); };
  $("#btn-applications").onclick = openApplications;
  $("#apps-close").onclick = () => $("#apps-dialog").close();
  $("#apps-list").onclick = (e) => {
    const b = e.target.closest("[data-open]");
    if (b) api("/api/open-folder", { method: "POST", body: { path: b.dataset.open } }).catch((err) => toast(err.message));
  };
}

// ---------------------------------------------------------------- boot
async function init() {
  [S.settings, S.status] = await Promise.all([api("/api/settings"), api("/api/status")]);
  try {
    const r = await api("/api/resume");
    S.master = r.data;
    S.masterYaml = r.yaml;
    if (r.error) toast(r.error, 10000);
  } catch { S.master = null; }
  fillSearchInputs();
  renderFilters();
  updateChips();
  renderResumePane();
  bindEvents();
  await loadJobs();
  if (S.status.rescoring) watchRescore();
  if (S.status.search?.running) pollSearch();
  else if (S.status.search?.finished_at) renderProgress(S.status.search);
  const m = S.status.models;
  if (!Object.keys(m.webllm || {}).length && !m.onnx) {
    $("#chip-model").className = "chip err";
    $("#chip-model").textContent = "Model files missing — run fetch_models.py";
  }
}

init().catch((e) => toast(`Could not start: ${e.message}`, 10000));
