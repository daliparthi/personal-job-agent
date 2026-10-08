import { EXTERNAL_ENGINES, LocalLLM } from "./llm.js";
import { refineWithModel, yamlHtml } from "./resume-parse.js";
import { changeSummary, esc, getAt, renderLetter, renderResume, setAt, textOf, updateBlock, valueFromText } from "./resume-view.js";
import { tailorResume } from "./tailor.js";
import { draftLetter } from "./coverletter.js";
import { initPipeline, openPipeline } from "./pipeline.js";
import { initSearches, selectedSearch, syncPicker } from "./searches.js";
import { initPanes } from "./panes.js";

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
// Statuses that mean you already applied (the pipeline tracks what happens after that).
// Job boards besides Workday (jobs.source). Autofill works on Workday sites only.
const SOURCES = { workday: "Workday", greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", smartrecruiters: "SmartRecruiters" };
const isWorkday = (j) => !j?.source || j.source === "workday";
const APPLIED_OR_LATER = new Set(["applied", "screening", "interviewing", "offer", "rejected", "withdrawn", "ghosted"]);
const STATES = { AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", PR: "Puerto Rico", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming" };

// master = master_resume.yaml data (never changed by tailoring);
// tailored = { resume: copy, changes: {path: rec}, letter?: cover letter + short answers (coverletter.js) }
// view = "master" | "tailored" | "letter"
const S = {
  settings: null, status: null, jobs: [], sel: null, detail: null, master: null, masterYaml: "", tailored: null,
  view: "master", approved: [], rejected: [], tailoring: false, abort: null,
  lastVisit: null, onlyNew: false, // postings first seen after lastVisit are "new since your last visit"
  industries: [], // [{name, companies}] of the company lists, for the Industries picker
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
const saveKeywords = debounce(async () => {
  await saveSettings({ mandatory: $("#mandatory").value, optional: $("#optional").value, omit: $("#omit").value });
  syncPicker(); // typed keywords may now match a saved search, or no longer do
}, 700);
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
  $("#chip-jobs").textContent = `${st.jobs ?? 0} jobs stored · last run ${last} · new postings kept ${st.retention_days ?? 5} days · database ${((st.db_bytes || 0) / 1048576).toFixed(1)} of ${Math.round((st.db_limit || 0) / 1048576)} MB`;
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
    const external = llm.backend === "remote";
    c.textContent = external ? `${label} ready` : `Qwen2.5‑0.5B ready · ${label}`;
    c.title = `${external ? `Answers come from ${llm.engineName} through this program.` : "Runs locally in this tab from the bundled model files."}${llm.gpuNote ? ` ${llm.gpuNote}.` : ""}`;
    b.hidden = true;
  } else if (state === "error") {
    c.className = "chip err";
    c.textContent = "Model failed to load";
    c.title = label;
    b.disabled = false;
  }
});

/** What the page is waiting for while the model loads. */
const loadingText = () => (S.settings.engine in EXTERNAL_ENGINES
  ? `Connecting to ${EXTERNAL_ENGINES[S.settings.engine]}…` : "Loading Qwen2.5‑0.5B from the bundled model files…");

async function loadModel() {
  try {
    const engine = S.settings.engine;
    await llm.load(S.status.models, engine in EXTERNAL_ENGINES || engine === "onnx" ? engine : "auto", S.settings.llm_model || "");
    if (engine in EXTERNAL_ENGINES && llm.backend !== "remote" && llm.gpuNote) toast(llm.gpuNote, 9000);
  } catch (e) {
    toast(`Model: ${e.message || e}`, 9000);
  }
}

// ---------------------------------------------------------------- search
function fillSearchInputs() {
  $("#mandatory").value = S.settings.mandatory || "";
  $("#optional").value = S.settings.optional || "";
  $("#omit").value = S.settings.omit || "";
}

async function loadIndustries() {
  try { S.industries = await api("/api/industries"); } catch { S.industries = []; }
  renderIndustries();
}

/** The Industries picker in the search bar: none picked searches (and lists) every industry's companies. */
function renderIndustries() {
  const sel = new Set(S.settings.industries || []);
  const picked = [...sel];
  const btn = $("#btn-industries");
  btn.textContent = !picked.length ? "All industries" : picked.length === 1 ? picked[0] : `${picked[0]} +${picked.length - 1}`;
  btn.title = picked.length ? `Searching and listing only: ${picked.join("; ")}` : "Searching every industry's companies";
  $("#industries-pop").innerHTML = `<div class="industries-grid">${S.industries.map((x) =>
    `<label><input type="checkbox" value="${esc(x.name)}"${sel.has(x.name) ? " checked" : ""}>${esc(x.name)}<span class="hint">${x.companies}</span></label>`).join("")
    || '<span class="hint">No company lists found.</span>'}</div>
    <p class="hint">Searches and the job list cover only these industries' companies. None ticked: all of them.</p>
    <div class="pop-actions"><button type="button" class="btn small" data-act="clear">All industries</button><button type="button" class="btn small primary" data-act="done">Done</button></div>`;
}

async function setIndustries(list) {
  await saveSettings({ industries: list });
  renderIndustries();
  syncPicker(); // the industries are part of a saved search
}

/** Run the keywords in the boxes; when they are a saved search's, the run is recorded under it. */
async function runSearch(searchId) {
  await saveSettings({ mandatory: $("#mandatory").value, optional: $("#optional").value, omit: $("#omit").value });
  if (!S.settings.mandatory.trim() && !S.settings.optional.trim()) return toast("Enter at least one keyword first.");
  syncPicker();
  const search_id = typeof searchId === "number" ? searchId : selectedSearch()?.id ?? null;
  try {
    await api("/api/search/run", { method: "POST", body: { full_refresh: $("#full-refresh").checked, search_id } });
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
  $("#f-knockouts").checked = !!f.hide_knockouts;
  $("#f-sponsor").value = f.sponsorship || "any";
  $("#f-sort").value = f.sort || "match_salary";
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

const SPONSOR_BADGES = { sponsors: "sponsors visas", h1b: "H-1B", h4ead: "H-4 EAD ok" };
const isNew = (j) => !!(S.lastVisit && j.first_seen && j.first_seen > S.lastVisit);

function renderJobs() {
  const fresh = S.jobs.filter(isNew).length;
  if (!fresh) S.onlyNew = false;
  const shown = S.onlyNew ? S.jobs.filter(isNew) : S.jobs;
  $("#list-count").textContent = `${shown.length} position${shown.length === 1 ? "" : "s"}`;
  const nb = $("#list-new");
  nb.hidden = !fresh;
  nb.textContent = S.onlyNew ? "show all" : `${fresh} new since your last visit`;
  if (!shown.length) {
    $("#job-list").innerHTML = `<div class="empty">${S.status?.jobs ? "No positions match the current filters." : "Upload your master resume, enter keywords, then run a job search."}</div>`;
    return;
  }
  $("#job-list").innerHTML = shown.map((j) => {
    const sc = j.match_score;
    const cls = sc == null ? "lo" : sc >= 45 ? "hi" : sc >= 25 ? "mid" : "lo";
    const where = j.states.length ? j.states.join(", ") : j.location;
    const badges = [`<span class="badge t-${esc(j.employment_type)}">${esc(j.employment_type)}</span>`];
    if (j.remote_type === "Remote" || j.remote_type === "Hybrid") badges.push(`<span class="badge b-remote">${j.remote_type}</span>`);
    if (j.salary_max) badges.push(`<span class="badge b-salary">${money(j.salary_min)}–${money(j.salary_max)}</span>`);
    j.optional_hits.forEach((k) => badges.push(`<span class="badge b-opt">${esc(k)}</span>`));
    if (j.status && j.status !== "new") badges.push(`<span class="badge b-status">${esc(j.status)}</span>`);
    if (isNew(j)) badges.unshift('<span class="badge b-new" title="Found since you last opened Job Agent">new</span>');
    (j.sponsorship || "").split(",").filter((t) => SPONSOR_BADGES[t]).forEach((t) => badges.push(`<span class="badge b-sponsor" title="The posting mentions it">${SPONSOR_BADGES[t]}</span>`));
    (j.knockouts || []).forEach((k) => badges.push(`<span class="badge b-ko" title="A hard requirement you don't meet">⛔ ${esc(k.label)}</span>`));
    if (!isWorkday(j)) badges.push(`<span class="badge b-src" title="Found on ${esc(SOURCES[j.source] || j.source)}">${esc(SOURCES[j.source] || j.source)}</span>`);
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
  const where = (k) => a.where?.[k] || "";
  const chips = [...a.matched.map((k) => `<span class="kw hit w-${esc(where(k))}" title="${esc(WHERE[where(k)] || "")}">${esc(k)}</span>`),
    ...a.missing.map((m) => `<span class="kw miss w-${esc(m.where || "")}" title="${esc([m.where_label, m.context].filter(Boolean).join(": "))}">${esc(m.keyword)}${m.where === "required" ? '<sup>req</sup>' : ""}</span>`)].join("");
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
    ${whyHtml(d)}
    <div class="legend"><span class="l-hit">in your resume</span><span class="l-miss">missing</span><span class="l-opt">optional keyword</span><span class="hint">faded: only "nice to have" · <sup>req</sup>: required</span></div>
    <div class="kw-chips">${chips || '<span class="hint">No ATS keywords detected.</span>'}</div>
    <div class="jd-body" id="jd-body"></div>`;
  const body = sanitize(d.description_html);
  const holder = $("#jd-body");
  holder.append(...body.childNodes);
  highlightJD(holder, d);
}

const WHERE = { required: "Required qualification", preferred: "Nice to have", responsibilities: "In the duties",
  intro: "In the overview", other: "Mentioned" };
const STATUS_ICON = { met: "✓", partial: "◐", missing: "✗", likely: "≈", unclear: "·" };
const STATUS_TITLE = { met: "Your resume has every skill this line names", partial: "Your resume has some of the skills this line names",
  missing: "Your resume names none of the skills on this line", likely: "No named skills; a resume line looks related", unclear: "No named skills to check" };

/** "Why this score": the base factors, every adjustment with its reason, hard requirements, and per-requirement evidence. */
function whyHtml(d) {
  const a = d.analysis;
  if (!a || a.base == null) return "";
  const bar = (label, v, w) => `<div class="why-bar" title="${label}: ${v}% (weighs ${w}%)"><span>${label}</span><i style="--v:${v}%"></i><b>${v}%</b></div>`;
  const adj = (a.adjustments || []).map((x) => `<li><b class="down">${x.points}</b> ${esc(x.reason)}</li>`).join("");
  const kos = (a.knockouts || []).map((k) => `<span class="kw ${k.blocking ? "ko" : "ko-soft"}" title="${esc(k.text || "")}">${k.blocking ? "⛔" : "⚠"} ${esc(k.label)}</span>`).join("");
  const facts = [];
  const ex = a.experience;
  if (ex?.need) facts.push(`Asks for <b>${ex.need}+ years</b>${ex.have != null ? `; your resume shows about <b>${ex.have}</b>` : ""}`);
  (ex?.skills || []).forEach((s) => facts.push(`${esc(s.skill)}: asks ${s.need}+ yrs${s.have != null ? `, you ~${s.have}` : ""}`));
  const sen = a.seniority;
  if (sen) facts.push(`${esc(sen.job_label.replace(/^an? /, "").replace(/^./, (c) => c.toUpperCase()))}; your latest title (${esc(sen.recent_title)}) reads as ${esc(sen.your_label.replace(/^an? /, ""))}`);
  const ev = a.evidence || [];
  const met = ev.filter((e) => e.status === "met" || e.status === "likely").length;
  const evRows = ev.map((e) => `<li class="ev ev-${e.status}" title="${esc(STATUS_TITLE[e.status])}"><span class="ev-icon">${STATUS_ICON[e.status]}</span>
      <div>${esc(e.text)}${e.kind === "preferred" ? ' <span class="hint">(nice to have)</span>' : ""}
      ${e.evidence ? `<div class="ev-from">From your resume: “${esc(e.evidence)}”</div>` : ""}</div></li>`).join("");
  const fb = d.feedback;
  return `<section class="why" aria-label="Why this match score">
    <div class="why-head"><b>Match ${a.score}</b><span class="hint">${a.adjustments?.length ? `base ${a.base}, adjusted below` : "keywords, wording and title"}</span>
      <span class="spacer"></span>
      <button type="button" class="fb${fb === 1 ? " on" : ""}" data-fb="1" title="A good match: tools/calibrate.py learns the score weights from these">👍</button>
      <button type="button" class="fb${fb === -1 ? " on" : ""}" data-fb="-1" title="A poor match">👎</button></div>
    <div class="why-bars">${bar("Keywords", a.coverage, 60)}${bar("Wording", a.similarity, 25)}${bar("Title", a.title_alignment, 15)}</div>
    ${adj ? `<ul class="why-adj">${adj}</ul>` : ""}
    ${kos ? `<div class="kw-chips">${kos}</div>` : ""}
    ${facts.length ? `<div class="why-facts">${facts.map((f) => `<div>${f}</div>`).join("")}</div>` : ""}
    ${ev.length ? `<details class="why-ev"><summary>Requirements: ${met} of ${ev.length} shown in your resume</summary><ul>${evRows}</ul></details>` : ""}
    ${a.boilerplate_share ? `<div class="hint">${a.boilerplate_share}% of this posting is about the company, pay and benefits; it doesn't count toward the score.</div>` : ""}
  </section>`;
}

async function setFeedback(value) {
  const d = S.detail;
  if (!d) return;
  const next = d.feedback === value ? 0 : value;
  try {
    const r = await api(`/api/jobs/${enc(d.id)}/feedback`, { method: "POST", body: { value: next } });
    d.feedback = r.feedback;
    renderJD();
  } catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- resume pane
const viewOpts = () => ({ diff: true, changes: S.view === "letter" ? S.tailored?.letter?.changes : S.tailored?.changes,
  editable: !S.tailoring, approved: S.approved });
// The document the lines on screen belong to: the tailored resume, or the cover letter (which holds its own changes).
const editTarget = () => (S.view === "letter"
  ? { data: S.tailored.letter, changes: S.tailored.letter.changes }
  : { data: S.tailored.resume, changes: S.tailored.changes });
const editableView = () => (S.view === "tailored" || (S.view === "letter" && !!S.tailored?.letter)) && !!S.tailored && !S.tailoring;

function renderLegend() {
  const el = $("#change-legend");
  const on = editableView();
  el.hidden = !on;
  if (!on) return;
  const c = changeSummary(editTarget().changes);
  const parts = [`<span class="lg-chg">${c.applied} change${c.applied === 1 ? "" : "s"}</span>`];
  if (c.check) parts.push(`<span class="lg-warn">${c.check} to check</span>`);
  if (c.held) parts.push(`<span class="lg-held">${c.held} AI version${c.held === 1 ? "" : "s"} held back</span>`);
  if (c.undone) parts.push(`<span class="lg-undone">${c.undone} undone</span>`);
  el.innerHTML = `${parts.join("")}<span class="hint">Hover a changed line for <b>Undo</b> at its end · click a line to edit it</span>`;
}

function renderResumePane() {
  const d = S.detail;
  $("#tab-tailored").disabled = !S.tailored;
  $("#tab-letter").disabled = !S.tailored;
  if (S.view !== "master" && !S.tailored) S.view = "master";
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === S.view));
  const view = $("#resume-view");
  if (S.view === "letter" && S.tailored) {
    renderLetter(view, S.tailored.letter, S.tailored.resume, viewOpts());
  } else if (S.view === "tailored" && S.tailored) {
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
    line.title = `Keyword coverage ${a.coverage}% · wording similarity ${a.similarity}% · title alignment ${a.title_alignment}%`
      + (a.adjustments?.length ? ` · adjustments ${a.adjustments.map((x) => x.points).join(", ")} (see "Why" in the job description pane)` : "");
    const delta = after - before;
    line.innerHTML = `Match <b>${before ?? "–"}</b>` + (after != null ? ` → <b>${after}</b> <span class="${delta >= 0 ? "up" : "down"}">${delta >= 0 ? "+" : ""}${delta}</span>` : "");
  } else line.innerHTML = "";
  const ready = !!(d && S.master);
  $("#btn-tailor").disabled = !ready || S.tailoring;
  $("#btn-tailor").textContent = S.tailored ? "Re-tailor for this position" : "Tailor my resume for this position only";
  $("#btn-tailor-stop").hidden = !S.tailoring;
  $("#btn-letter").disabled = !S.tailored || S.tailoring;
  $("#btn-letter").textContent = S.tailored?.letter ? "Rewrite cover letter" : "Write cover letter";
  $("#btn-package").disabled = !S.tailored || S.tailoring;
  $("#btn-apply").disabled = !S.tailored || S.tailoring;
  $("#btn-apply").textContent = isWorkday(d) ? "Apply with autofill" : "Open posting";
  $("#btn-apply").title = isWorkday(d) ? "Opens the posting in a separate browser window and fills standard fields"
    : `Saves the package and opens the ${SOURCES[d.source] || d.source} posting in a separate browser window (autofill works on Workday sites only)`;
  $("#btn-applied").disabled = !d || !d.folder || APPLIED_OR_LATER.has(d.status);
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
  if (!S.tailored) return;
  const { data, changes } = editTarget();
  const rec = changes[path];
  if (!rec) return;
  const toOrig = rec.state === "alt";
  rec.state = toOrig ? "orig" : "alt";
  setAt(data, path, structuredClone(toOrig ? rec.orig : rec.alt));
  if (!toOrig && rec.held) { rec.warn = `you chose the AI version, which ${rec.held.replace(/^adds/, "added").replace(/^uses/, "used")}`; delete rec.held; }
  if (!updateBlock($("#resume-view"), path, data, viewOpts())) renderResumePane();
  renderLegend();
  persistEdits();
}

/** You typed in a line of the tailored resume or the cover letter. */
function onLineEdited(path, text) {
  const { data, changes } = editTarget();
  const value = valueFromText(path, text);
  let rec = changes[path];
  if (!rec) rec = changes[path] = { orig: structuredClone(getAt(data, path)), alt: value, state: "alt" };
  setAt(data, path, value);
  if (textOf(value) === textOf(rec.orig)) delete changes[path];
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
        <div class="kw-name">${esc(it.keyword)}${it.soft ? ' <span class="kw-kind" title="Soft skills get their own sentence in the summary, never next to a tool">soft skill · own sentence in the summary</span>' : ""}</div>
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
    setTailorStatus(loadingText());
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
  const letter = S.tailored?.letter; // a cover letter already written stays (rewrite it for the new resume if you like)
  let result;
  try {
    result = await tailorResume({
      llm, master: S.master, job, analysis: job.analysis, approved: S.approved,
      maxBullets: Number(S.settings.max_bullets ?? 12), signal: S.abort.signal,
      ui: {
        onRender: (res, changes) => { S.tailored = { resume: res, changes, ...(letter ? { letter } : {}) }; renderResumePane(); },
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
  const doc = { resume: result.res, changes: result.changes, ...(letter ? { letter } : {}) };
  S.tailored = doc;
  const sc = await api(`/api/jobs/${enc(job.id)}/score`, { method: "POST", body: { resume: doc.resume } });
  await api(`/api/jobs/${enc(job.id)}/tailored`, { method: "PUT", body: { doc, score_after: sc.score, approved: S.approved, rejected: S.rejected } });
  job.tailored = { doc, score_after: sc.score, approved: S.approved, rejected: S.rejected };
  if (job.status === "new" || !job.status) job.status = "tailored";
  const st = result.stats;
  const extra = [st.flagged ? `${st.flagged} marked amber to check` : "", st.held ? `${st.held} AI version(s) held back because they added facts you didn't approve` : ""].filter(Boolean).join(", ");
  const added = st.added ? `, added ${st.added} new sentence(s) for your approved keywords` : "";
  setTailorStatus(`Done in ${st.seconds.toFixed(0)}s — rewrote ${st.rewritten} line(s)${added}${extra ? ` (${extra})` : ""}. Hover any changed line to undo it.`, false);
  renderResumePane();
  await loadJobs();
  toast(`Match ${job.match_score} → ${sc.score}. Review the green edits before applying.`);
}

// ---------------------------------------------------------------- cover letter + short answers
async function onWriteLetter() {
  if (!S.detail || !S.tailored || S.tailoring) return;
  if (S.tailored.letter && Object.values(S.tailored.letter.changes || {}).some((r) => r.edited)
      && !confirm("Rewrite the cover letter? Your edits to the current one will be replaced.")) return;
  if (llm.state !== "ready") {
    setTailorStatus(loadingText());
    await loadModel();
    if (llm.state !== "ready") return setTailorStatus("");
  }
  const job = S.detail;
  S.tailoring = true;
  S.abort = new AbortController();
  S.view = "letter";
  const view = $("#resume-view");
  let result;
  try {
    result = await draftLetter({
      llm, resume: S.tailored.resume, job, analysis: job.analysis, approved: S.approved, digest: job.digest || {},
      signal: S.abort.signal,
      ui: {
        onRender: (letter) => { S.tailored.letter = letter; renderResumePane(); },
        onBlock: (path) => { if (!updateBlock(view, path, S.tailored.letter, viewOpts())) renderResumePane(); },
        onStatus: (t) => setTailorStatus(llm.backend === "onnx" ? `${t} (CPU model: the page pauses a few seconds as each paragraph starts)` : t),
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
    return toast(`Writing the cover letter failed: ${e.message || e}`, 8000);
  }
  S.tailoring = false;
  S.tailored.letter = result.letter;
  await api(`/api/jobs/${enc(job.id)}/tailored`, { method: "PUT", body: { doc: S.tailored, score_after: job.tailored?.score_after, approved: S.approved, rejected: S.rejected } });
  job.tailored = { ...(job.tailored || {}), doc: S.tailored };
  const st = result.stats;
  const extra = [st.flagged ? `${st.flagged} marked amber to check` : "", st.held ? `${st.held} AI version(s) held back because they added facts` : ""].filter(Boolean).join(", ");
  setTailorStatus(`Done in ${st.seconds.toFixed(0)}s — the AI wrote ${st.rewritten} paragraph(s) and answer(s)${extra ? ` (${extra})` : ""}. The rest is built from your resume lines. Saved with the package.`, false);
  renderResumePane();
}

async function copyAnswer(i) {
  const a = S.tailored?.letter?.answers?.[i];
  if (!a) return;
  try {
    await navigator.clipboard.writeText(a.text);
    toast("Answer copied.");
  } catch { toast("Copy failed: select the text and copy it yourself."); }
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
    let msg = `Saved to ${r.folder}${r.cover_letter ? " (with the cover letter)" : ""}`;
    if (r.pdf_error) msg += ` (PDF skipped: ${r.pdf_error})`;
    if (launch) msg = !r.launched ? `Saved the package, but the browser could not open: ${r.launch_error}`
      : isWorkday(S.detail) ? "Opened the posting in a separate browser window. Sign in if asked; standard fields fill automatically. Review, then click Submit yourself."
      : `Opened the posting in a separate browser window. Fill in the ${SOURCES[S.detail.source] || ""} form with the files in ${r.folder}, submit it yourself, then click “Mark applied”.`;
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
  if (!APPLIED_OR_LATER.has(S.detail.status)) S.detail.status = "applied";
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
  $("#yaml-intro").innerHTML = `The model reads each part of <b>${esc(filename)}</b> and writes it as YAML: the usual fields where they fit, and any other labelled line (like “Technologies:”) as a field of its own. A part is used only when every value in it is in your resume (a small typo is put back to your own wording) and nothing is left out; otherwise it keeps the quick parse. So it can't invent anything.`;
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
    status.innerHTML = `<span class="dot"></span>${esc(loadingText())}`;
    await loadModel();
  }
  if (!dlg.open) return;
  if (llm.state === "ready" && !abort.signal.aborted) {
    const r = await refineWithModel({
      llm, draft, signal: abort.signal, check: (body) => api("/api/resume/loose", { method: "POST", body }), ui: {
        onStatus: (t) => { status.innerHTML = `<span class="dot"></span>${esc(t)} <span class="hint" id="yaml-rate"></span>`; },
        onTokens: (n) => { const el = $("#yaml-rate"); if (el) el.textContent = `${n} tokens · ${llm.label}`; },
        onUpdate: show,
      },
    });
    if (!dlg.open) return;
    if (r.done) how = `read as YAML by ${llm.backend === "remote" ? llm.label : `Qwen2.5-0.5B, ${llm.label}`}`;
    const keptNote = r.kept.length ? ` ${r.kept.length} part(s) kept the quick parse (${r.kept[0]}${r.kept.length > 1 ? "; …" : ""}).` : "";
    status.textContent = abort.signal.aborted
      ? `Stopped. The rest uses the quick rule-based parse.${keptNote}`
      : `Done in ${Math.round((performance.now() - started) / 1000)}s. The model wrote ${r.parts} part(s) of your resume as YAML${r.used ? ` and filled ${r.used} heading field(s)` : ""}.${keptNote}`;
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
  ["us_citizen", "US citizen? (blank: don't say)", ["", "Yes", "No"]],
  ["has_clearance", "Security clearance? (blank: don't say)", ["", "Yes", "No"]],
];

function renderAccount() {
  const a = S.status.account || {};
  const ok = (v) => `<span class="${v ? "good" : "bad"}">${v ? "set" : "not set"}</span>`;
  const where = a.password_in_keychain ? " (OS keychain)" : a.password_in_env ? " (.env file)" : "";
  $("#home-path").textContent = S.status.home || "";
  $("#env-path").textContent = a.path || "";
  $("#env-status").innerHTML = `WORKDAY_EMAIL: ${a.email ? `<b>${esc(a.email)}</b>` : ok(false)} · WORKDAY_PASSWORD: ${ok(a.password_set)}${where}`
    + (a.company_overrides?.length ? ` · company-specific: ${esc(a.company_overrides.join(", "))}` : "");
  $("#keychain-row").hidden = !a.keychain;
  $("#btn-kc-move").hidden = !(a.keychain && a.env_passwords?.length);
  $("#kc-hint").textContent = !a.keychain
    ? "This computer has no OS keychain Job Agent can use, so keep the password in the .env file."
    : a.env_passwords?.length
      ? `${a.env_passwords.join(", ")} ${a.env_passwords.length === 1 ? "is" : "are"} in plain text in .env: move ${a.env_passwords.length === 1 ? "it" : "them"} to the keychain.`
      : "Passwords saved here go to your OS keychain (Windows Credential Manager, macOS Keychain or Linux Secret Service), not to a file. An empty password deletes the saved one.";
}

const ENGINE_URLS = { ollama: "http://localhost:11434", openai: "https://api.openai.com/v1", anthropic: "https://api.anthropic.com" };

/** Show the server address, model and key rows only for an external engine; the key only where one is needed. */
function syncEngineRows() {
  const form = $("#settings-form");
  const engine = form.engine.value;
  const external = engine in EXTERNAL_ENGINES;
  $("#llm-external").hidden = !external;
  if (!external) return;
  if (engine !== S.settings.engine) { // another engine: its usual address, and a model still to be chosen
    form.llm_url.value = "";
    form.llm_model.innerHTML = '<option value=""></option>';
  }
  form.llm_url.placeholder = ENGINE_URLS[engine];
  const needsKey = engine !== "ollama";
  $("#llm-key-row").hidden = !needsKey;
  const has = !!S.status?.account?.api_keys?.[engine];
  $("#llm-key").placeholder = has ? "API key saved (type a new one to replace it)" : "API key";
  $("#btn-llm-key").textContent = has ? "Replace key (empty deletes it)" : "Save key in OS keychain";
}

async function saveLlmKey() {
  const engine = $("#settings-form").engine.value;
  try {
    const key = $("#llm-key").value.trim();
    S.status.account = await api("/api/llm/key", { method: "POST", body: { engine, key } });
    $("#llm-key").value = "";
    syncEngineRows();
    toast(key ? `Saved the ${EXTERNAL_ENGINES[engine]} key in the OS keychain.` : `Deleted the ${EXTERNAL_ENGINES[engine]} key.`);
  } catch (e) { toast(e.message, 8000); }
}

/** Save the engine choice, ask the server which models it has, and list them. */
async function checkLlm() {
  const form = $("#settings-form");
  const status = $("#llm-status");
  status.textContent = "Checking…";
  try {
    await saveSettings({ engine: form.engine.value, llm_url: form.llm_url.value.trim(), llm_model: form.llm_model.value });
    const { models } = await api("/api/llm/models");
    const keep = form.llm_model.value;
    form.llm_model.innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    form.llm_model.value = models.includes(keep) ? keep : models[0] || "";
    status.textContent = models.length ? `Connected: ${models.length} model(s). Choose one, then Save.` : "Connected, but it has no models yet.";
  } catch (e) { status.textContent = `Not connected: ${e.message}`; }
}

async function saveKeychainPassword() {
  const company = $("#kc-company").value.trim();
  try {
    const password = $("#kc-password").value;
    S.status.account = await api("/api/account/password", { method: "POST", body: { password, company } });
    $("#kc-password").value = "";
    renderAccount();
    const whose = company ? `${company}'s` : "your";
    toast(password ? `Saved ${whose} Workday password in the OS keychain.` : `Deleted ${whose} Workday password from the OS keychain.`);
  } catch (e) { toast(e.message, 8000); }
}

async function moveEnvPasswords() {
  try {
    const r = await api("/api/account/move-to-keychain", { method: "POST" });
    S.status.account = r.account;
    renderAccount();
    toast(r.moved.length ? `Moved ${r.moved.join(", ")} to the OS keychain and blanked ${r.moved.length === 1 ? "it" : "them"} in .env.` : "Nothing to move.");
  } catch (e) { toast(e.message, 8000); }
}

async function openSettings() {
  const s = S.settings;
  const form = $("#settings-form");
  S.status = await api("/api/status");
  renderAccount();
  form.current_employer.value = s.current_employer || "";
  form.engine.value = s.engine === "onnx" || s.engine in EXTERNAL_ENGINES ? s.engine : "auto";
  form.llm_url.value = s.llm_url || "";
  form.llm_model.innerHTML = `<option value="${esc(s.llm_model || "")}">${esc(s.llm_model || "")}</option>`;
  $("#llm-status").textContent = "";
  syncEngineRows();
  form.upload_format.value = s.upload_format || "docx";
  form.max_bullets.value = s.max_bullets ?? 12;
  form.keep_new_days.value = s.keep_new_days ?? 5;
  form.max_db_mb.value = s.max_db_mb ?? 50;
  form.ghost_after_days.value = s.ghost_after_days ?? 21;
  $("#profile-fields").innerHTML = PROFILE_FIELDS.map(([k, label, opts]) => {
    const v = s.profile[k] ?? "";
    const input = opts
      ? `<select name="p_${k}">${opts.map((o) => `<option value="${esc(o)}"${o === v ? " selected" : ""}>${esc(o || "—")}</option>`).join("")}</select>`
      : `<input name="p_${k}" value="${esc(v)}" autocomplete="off">`;
    return `<label>${esc(label)}${input}</label>`;
  }).join("");
  const af = s.autofill || {};
  form.af_experience.checked = af.experience !== false;
  form.af_add_entries.checked = !!af.add_entries;
  form.af_answers.checked = af.answers !== false;
  form.af_capture.checked = af.capture !== false;
  $("#disclosure-fields").innerHTML = DISCLOSURES.map(([k, label, answers]) => {
    const have = (s.disclosures || {})[k] || "decline";
    const opts = [["decline", "Decline to answer"], ["skip", "Leave for me"], ...answers];
    return `<label>${esc(label)}<select name="d_${k}">`
      + opts.map(([v, text]) => `<option value="${v}"${v === have ? " selected" : ""}>${esc(text)}</option>`).join("") + "</select></label>";
  }).join("");
  api("/api/answers").then((list) => { $("#answers-count").textContent = `${list.length} saved answer${list.length === 1 ? "" : "s"}`; });
  await renderCompanies();
  $("#settings-dialog").showModal();
}

// ---------------------------------------------------------------- answer bank
// [key, label, [[answer, text]...]]; the answer keys are the ones autofill_rules.json ("disclosure_options") knows.
const DISCLOSURES = [
  ["gender", "Gender", [["male", "Male"], ["female", "Female"]]],
  ["ethnicity", "Ethnicity / race", [["asian", "Asian"], ["white", "White"], ["black", "Black or African American"],
    ["hispanic", "Hispanic or Latino"], ["native", "American Indian or Alaska Native"],
    ["pacific", "Native Hawaiian or Pacific Islander"], ["two_or_more", "Two or more races"]]],
  ["veteran", "Veteran status", [["not_veteran", "I am not a protected veteran"], ["veteran", "I am a protected veteran"]]],
  ["disability", "Disability", [["yes", "Yes, I have a disability"], ["no", "No, I don't have a disability"]]],
];

async function openAnswers() {
  await renderAnswers();
  $("#answers-dialog").showModal();
}

async function renderAnswers() {
  const list = await api("/api/answers");
  $("#answers-count").textContent = `${list.length} saved answer${list.length === 1 ? "" : "s"}`;
  const when = (iso) => (iso || "").slice(0, 10);
  $("#ab-list").innerHTML = list.length ? list.map((a) => `
    <div class="ab-row" data-id="${a.id}">
      <div><div class="q">${esc(a.question)}</div>
        <textarea rows="2" data-answer="${a.id}">${esc(a.answer)}</textarea>
        <div class="sub">${a.kind === "choice" ? "option from a list · " : ""}${esc(a.source)}${a.company ? ` · ${esc(a.company)}` : ""} · updated ${esc(when(a.updated_at))}${a.uses ? ` · used ${a.uses}×` : ""}</div></div>
      <div><button type="button" class="linkish" data-del="${a.id}">Delete</button></div>
    </div>`).join("") : '<p class="hint">No answers yet. Answer a custom question in the apply window, or add one below.</p>';
}

async function onAnswersEvent(e) {
  const del = e.target.closest("[data-del]");
  if (del && e.type === "click") {
    if (!confirm("Delete this answer?")) return;
    await api(`/api/answers/${del.dataset.del}`, { method: "DELETE" });
    return renderAnswers();
  }
  const ta = e.target.closest("[data-answer]");
  if (ta && e.type === "change") {
    try {
      await api(`/api/answers/${ta.dataset.answer}`, { method: "PUT", body: { answer: ta.value.trim() } });
      toast("Answer saved.");
    } catch (err) { toast(err.message, 7000); }
  }
}

async function addAnswer() {
  const question = $("#ab-q").value.trim(), answer = $("#ab-a").value.trim();
  if (!question || !answer) return toast("Enter a question and its answer.");
  try {
    await api("/api/answers", { method: "POST", body: { question, answer, kind: $("#ab-choice").checked ? "choice" : "text" } });
    $("#ab-q").value = $("#ab-a").value = "";
    $("#ab-choice").checked = false;
    await renderAnswers();
  } catch (err) { toast(err.message, 8000); }
}

async function renderCompanies() {
  const list = await api("/api/companies");
  const ok = list.filter((c) => !c.error);
  const yours = ok.filter((c) => c.source === "yours").length;
  $("#companies-count").textContent = `${ok.filter((c) => c.active).length} of ${ok.length} searched · ${ok.length - yours} shared, ${yours} yours`;
  const rowOf = (c) => (c.error
    ? `<div class="err" title="${esc(c.url)}">${esc(c.name || c.url)}: ${esc(c.error)}</div>`
    : `<label title="${esc(c.url)}${c.enabled ? "" : " (enabled: false in the file)"}"><input type="checkbox" data-key="${esc(c.key)}"${c.active ? " checked" : ""}${c.enabled ? "" : " disabled"}> ${esc(c.name)}${c.ats && c.ats !== "workday" ? ` <span class="hint">· ${esc(SOURCES[c.ats] || c.ats)}</span>` : ""}${c.source === "yours" ? ' <span class="hint">(yours)</span>' : ""}</label>`);
  const groups = new Map(); // industry -> its companies, under a heading each
  list.forEach((c) => { const k = c.error ? "" : c.industry || "Other"; groups.set(k, [...(groups.get(k) || []), c]); });
  const names = [...groups.keys()].filter(Boolean).sort((a, b) => (a === "Other") - (b === "Other") || a.localeCompare(b));
  $("#companies").innerHTML = [...(groups.get("") || []).map(rowOf), ...names.map((n) =>
    `<div class="co-industry">${esc(n)} <span class="hint">(${groups.get(n).length})</span></div>${groups.get(n).map(rowOf).join("")}`)].join("")
    || '<span class="hint">No companies yet: add career sites to a file in the companies folder, or use the Add box.</span>';
  $("#new-co-industry").innerHTML = [...names.filter((n) => n !== "Other"), "Other"]
    .map((n) => `<option value="${esc(n === "Other" ? "" : n)}">${esc(n)}</option>`).join("");
}

async function saveSettingsDialog() {
  const form = $("#settings-form");
  const profile = {};
  PROFILE_FIELDS.forEach(([k]) => { profile[k] = form[`p_${k}`].value.trim(); });
  const disabled = $$("#companies input[type=checkbox]").filter((c) => !c.checked).map((c) => c.dataset.key);
  const engineChanged = form.engine.value !== S.settings.engine;
  await saveSettings({
    current_employer: form.current_employer.value.trim(), engine: form.engine.value,
    llm_url: form.llm_url.value.trim(), llm_model: form.llm_model.value,
    upload_format: form.upload_format.value, max_bullets: Number(form.max_bullets.value || 12),
    keep_new_days: Number(form.keep_new_days.value || 5), max_db_mb: Number(form.max_db_mb.value || 50),
    ghost_after_days: Number(form.ghost_after_days.value || 21),
    profile, disabled_companies: disabled,
    autofill: { experience: form.af_experience.checked, add_entries: form.af_add_entries.checked,
                answers: form.af_answers.checked, capture: form.af_capture.checked },
    disclosures: Object.fromEntries(DISCLOSURES.map(([k]) => [k, form[`d_${k}`].value])),
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
  $("#btn-run").onclick = () => runSearch();
  $("#list-new").onclick = () => { S.onlyNew = !S.onlyNew; renderJobs(); };
  $("#btn-stop").onclick = () => api("/api/search/stop", { method: "POST" });
  $("#btn-log").onclick = () => { $("#log").hidden = !$("#log").hidden; };
  $("#mandatory").addEventListener("input", saveKeywords);
  $("#optional").addEventListener("input", saveKeywords);
  $("#omit").addEventListener("input", saveKeywords);
  [$("#mandatory"), $("#optional"), $("#omit")].forEach((el) => el.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); }));
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
  $("#f-knockouts").onchange = () => saveFilter({ hide_knockouts: $("#f-knockouts").checked });
  $("#f-sponsor").onchange = () => saveFilter({ sponsorship: $("#f-sponsor").value });
  $("#f-sort").onchange = () => saveFilter({ sort: $("#f-sort").value });
  $("#jd-view").addEventListener("click", (e) => { const b = e.target.closest("[data-fb]"); if (b) setFeedback(Number(b.dataset.fb)); });
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
  $("#btn-industries").onclick = () => {
    const pop = $("#industries-pop");
    pop.hidden = !pop.hidden;
    $("#btn-industries").setAttribute("aria-expanded", String(!pop.hidden));
  };
  $("#industries-pop").onclick = async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "clear") $$("#industries-pop input").forEach((c) => (c.checked = false));
    if (act) {
      $("#industries-pop").hidden = true;
      $("#btn-industries").setAttribute("aria-expanded", "false");
      await setIndustries($$("#industries-pop input:checked").map((c) => c.value));
    }
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".states") && !$("#states-pop").hidden) $("#states-pop").querySelector('[data-act="done"]').click();
    if (!e.target.closest(".industries") && !$("#industries-pop").hidden) $("#industries-pop").querySelector('[data-act="done"]').click();
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
  $("#btn-letter").onclick = onWriteLetter;
  $("#btn-package").onclick = () => onPackage(false);
  $("#btn-apply").onclick = () => onPackage(true);
  $("#btn-applied").onclick = onMarkApplied;
  const rv = $("#resume-view");
  // Keep focus where it is so the line isn't repainted under the pointer before the click lands.
  rv.addEventListener("mousedown", (e) => { if (e.target.closest(".chg-ctl")) e.preventDefault(); });
  rv.addEventListener("click", (e) => {
    const copy = e.target.closest(".qa-copy");
    if (copy) return copyAnswer(Number(copy.dataset.copy));
    const b = e.target.closest(".chg-ctl");
    if (!b || !editableView()) return;
    e.preventDefault();
    toggleChange(b.closest("[data-row]").dataset.row);
  });
  rv.addEventListener("input", (e) => {
    const el = e.target.closest("[data-path]");
    if (!el || !editableView()) return;
    onLineEdited(el.dataset.path, el.innerText.replace(/\s+/g, " ").trim());
  });
  rv.addEventListener("focusout", (e) => {
    const el = e.target.closest?.("[data-path]");
    if (!el || !editableView()) return;
    updateBlock(rv, el.dataset.path, editTarget().data, viewOpts());
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
      await api("/api/companies", { method: "POST", body: { name: $("#new-co-name").value, url: $("#new-co-url").value, industry: $("#new-co-industry").value } });
      $("#new-co-name").value = $("#new-co-url").value = "";
      await renderCompanies();
      await loadIndustries();
      toast("Added to my_companies.yaml in your personal folder");
    } catch (e) { toast(e.message, 7000); }
  };
  $("#btn-open-home").onclick = () => api("/api/open", { method: "POST", body: { what: "home" } }).catch((err) => toast(err.message));
  $("#btn-open-env").onclick = () => api("/api/open", { method: "POST", body: { what: "env" } }).catch((err) => toast(err.message));
  $("#btn-env-refresh").onclick = async () => { S.status = await api("/api/status"); renderAccount(); };
  $("#btn-kc-save").onclick = saveKeychainPassword;
  $("#settings-form").engine.addEventListener("change", syncEngineRows);
  $("#btn-llm-check").onclick = checkLlm;
  $("#btn-llm-key").onclick = saveLlmKey;
  $("#llm-key").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); saveLlmKey(); } });
  $("#btn-kc-move").onclick = moveEnvPasswords;
  // Enter in the password box saves it (instead of submitting, and closing, the Settings dialog).
  ["#kc-password", "#kc-company"].forEach((s) => $(s).addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveKeychainPassword(); }
  }));
  $("#btn-applications").onclick = openApplications;
  $("#btn-pipeline").onclick = openPipeline;
  $("#apps-close").onclick = () => $("#apps-dialog").close();
  $("#btn-answers").onclick = openAnswers;
  $("#ab-close").onclick = () => $("#answers-dialog").close();
  $("#ab-add").onclick = addAnswer;
  $("#ab-list").addEventListener("click", onAnswersEvent);
  $("#ab-list").addEventListener("change", onAnswersEvent);
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
  await loadIndustries();
  updateChips();
  renderResumePane();
  bindEvents();
  initPanes();
  try { S.lastVisit = (await api("/api/visit", { method: "POST" })).previous; } catch { /* no "new" badges */ }
  await initSearches({
    api, toast,
    getKeywords: () => ({ mandatory: $("#mandatory").value, optional: $("#optional").value, omit: $("#omit").value,
      industries: S.settings.industries || [] }),
    setKeywords: async (mandatory, optional, omit = "", industries = []) => {
      $("#mandatory").value = mandatory;
      $("#optional").value = optional;
      $("#omit").value = omit;
      await saveSettings({ mandatory, optional, omit, industries });
      renderIndustries();
    },
    industries: () => S.industries,
    runSearch: (id) => runSearch(id),
    selectJob: (id) => selectJob(id),
    onSearchRunning: () => pollSearch(),
  });
  initPipeline({
    api, toast,
    onShowJob: (id) => selectJob(id),
    onChanged: async () => { // statuses may have changed on the board
      await loadJobs();
      if (S.sel && !S.tailoring) await selectJob(S.sel);
    },
  });
  await loadJobs();
  if (S.status.rescoring) watchRescore();
  if (S.status.search?.running) pollSearch();
  else if (S.status.search?.finished_at) renderProgress(S.status.search);
  const m = S.status.models;
  if (!Object.keys(m.webllm || {}).length && !m.onnx && !(S.settings.engine in EXTERNAL_ENGINES)) {
    $("#chip-model").className = "chip err";
    $("#chip-model").textContent = "Model files missing — run fetch_models.py";
  }
}

init().catch((e) => toast(`Could not start: ${e.message}`, 10000));
