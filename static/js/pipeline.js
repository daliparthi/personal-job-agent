// The Pipeline board: every job you worked on, by status, with follow-ups, notes and a timeline.
// Drag a card to another column (or use the Status menu in its details) to move it; Job Agent itself only ever
// moves jobs forward (tailored → saved → applying → applied) as you work.

import { esc } from "./resume-view.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const enc = (id) => id.split("/").map(encodeURIComponent).join("/");
const PREPARING = new Set(["tailored", "saved", "applying"]);

const day = (iso) => (iso ? new Date(iso.length > 10 ? iso : `${iso}T12:00:00`) : null);
const fmtDate = (iso) => (iso ? day(iso).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "");
const fmtWhen = (iso) => (iso ? day(iso).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "");

let deps = null;   // { api, toast, onShowJob, onChanged }
let board = null;
let sel = null;    // selected job id
let dragId = null;

export function initPipeline(d) {
  deps = d;
  const dlg = $("#pipeline-dialog");
  $("#pl-close").onclick = () => dlg.close();
  dlg.addEventListener("close", () => deps.onChanged?.());
  const b = $("#pl-board");
  b.addEventListener("click", (e) => { const c = e.target.closest(".pl-card"); if (c) select(c.dataset.id); });
  b.addEventListener("keydown", (e) => { const c = e.target.closest(".pl-card"); if (c && e.key === "Enter") select(c.dataset.id); });
  b.addEventListener("dragstart", (e) => {
    const c = e.target.closest(".pl-card");
    if (!c) return;
    dragId = c.dataset.id;
    e.dataTransfer.setData("text/plain", dragId);
    e.dataTransfer.effectAllowed = "move";
  });
  b.addEventListener("dragover", (e) => {
    const col = e.target.closest(".pl-col");
    if (!col || !dragId) return;
    e.preventDefault();
    $$(".pl-col.drop").forEach((x) => x !== col && x.classList.remove("drop"));
    col.classList.add("drop");
  });
  b.addEventListener("dragleave", (e) => { const col = e.target.closest(".pl-col"); if (col && !col.contains(e.relatedTarget)) col.classList.remove("drop"); });
  b.addEventListener("dragend", () => { dragId = null; $$(".pl-col.drop").forEach((x) => x.classList.remove("drop")); });
  b.addEventListener("drop", async (e) => {
    const col = e.target.closest(".pl-col");
    const id = dragId || e.dataTransfer.getData("text/plain");
    $$(".pl-col.drop").forEach((x) => x.classList.remove("drop"));
    dragId = null;
    if (!col || !id) return;
    e.preventDefault();
    const job = findJob(id);
    if (!job || columnOf(id) === col.dataset.col) return;
    await move(id, col.dataset.drop);
  });
  $("#pl-due").addEventListener("click", (e) => { const o = e.target.closest("[data-open]"); if (o) select(o.dataset.open); });
  $("#pl-detail").addEventListener("click", onDetailClick);
  $("#pl-detail").addEventListener("change", (e) => { if (e.target.id === "pl-status") move(sel, e.target.value); });
}

export async function openPipeline() {
  const dlg = $("#pipeline-dialog");
  if (!dlg.open) dlg.showModal();
  await refresh();
}

async function refresh() {
  let st;
  try {
    [board, st] = await Promise.all([deps.api("/api/pipeline"), deps.api("/api/pipeline/stats")]);
  } catch (e) { return deps.toast(e.message, 8000); }
  renderStats(st);
  renderDue();
  renderBoard();
  if (sel && findJob(sel)) await select(sel);
  else closeDetail();
}

function findJob(id) {
  for (const c of board?.columns || []) {
    const j = c.jobs.find((x) => x.id === id);
    if (j) return j;
  }
  return null;
}

const columnOf = (id) => board.columns.find((c) => c.jobs.some((j) => j.id === id))?.key;
const label = (s) => esc(board?.labels?.[s] || s || "");

async function move(id, status) {
  try {
    await deps.api(`/api/jobs/${enc(id)}/stage`, { method: "PATCH", body: { status } });
  } catch (e) { return deps.toast(e.message, 8000); }
  sel = id;
  await refresh();
}

// ---------------------------------------------------------------- header: statistics, follow-ups
function renderStats(s) {
  const pct = (v) => (v == null ? "–" : `${v}%`);
  const max = Math.max(1, ...s.per_week.map((w) => w.applied));
  const bars = s.per_week.map((w) =>
    `<span style="height:${Math.max(4, Math.round((100 * w.applied) / max))}%" class="${w.applied ? "" : "zero"}" title="Week of ${esc(fmtDate(w.week))}: ${w.applied} applied"></span>`).join("");
  const byScore = s.by_score.filter((b) => b.applied)
    .map((b) => `<span title="${b.interviews} of ${b.applied} applications with a match score of ${esc(b.bucket)} got an interview">${esc(b.bucket)}: <b>${b.rate}%</b></span>`).join("");
  $("#pl-stats").innerHTML = `
    <div class="pl-stat"><b>${s.applied}</b>applied</div>
    <div class="pl-stat" title="Applications that got any answer: a recruiter screen, an interview, an offer or a rejection"><b>${pct(s.response_rate)}</b>responses</div>
    <div class="pl-stat" title="Applications that reached an interview"><b>${pct(s.interview_rate)}</b>interviews</div>
    <div class="pl-stat"><b>${s.offers}</b>offer${s.offers === 1 ? "" : "s"}</div>
    <div class="pl-stat" title="Applied, at the recruiter screen or interviewing"><b>${s.waiting}</b>in progress</div>
    <div class="pl-stat"><div class="pl-weeks" aria-label="Applications per week">${bars}</div>per week · ${s.per_week.length} wks</div>
    ${byScore ? `<div class="pl-stat pl-by-score" title="Interview rate by match score (the tailored score when there is one)">${byScore}<div>interviews by match score</div></div>` : ""}`;
}

function renderDue() {
  const el = $("#pl-due");
  el.hidden = !board.due.length;
  el.innerHTML = board.due.length ? `<b>Follow-ups due</b> ${board.due.map((j) =>
    `<button type="button" class="linkish" data-open="${esc(j.id)}">${esc(j.next_action || "Follow up")}: ${esc(j.title)} at ${esc(j.company)} (${esc(fmtDate(j.next_action_at))})</button>`).join(" · ")}` : "";
}

// ---------------------------------------------------------------- the board
function card(j) {
  const flags = [];
  const score = j.score_after ?? j.match_score;
  if (PREPARING.has(j.status)) flags.push(`<span class="flag">${label(j.status)}</span>`);
  if (score != null) flags.push(`<span class="flag" title="${j.score_after != null ? "Match of the tailored resume" : "Match with your master resume"}">match ${score}</span>`);
  if (j.days_in_status != null) flags.push(`<span class="flag" title="Days since the last status change">${j.days_in_status}d</span>`);
  if (j.follow_up_due) flags.push(`<span class="flag warn" title="${esc(j.next_action || "Follow up")}">follow up</span>`);
  else if (j.next_action_at) flags.push(`<span class="flag" title="${esc(j.next_action || "Follow up")}">⏰ ${esc(fmtDate(j.next_action_at))}</span>`);
  if (j.suggest_ghosted) flags.push(`<span class="flag warn" title="No update for ${j.days_in_status} days. Move it to Ghosted?">quiet</span>`);
  if (j.closed_at) flags.push(`<span class="flag bad" title="Removed from the company's career site (${esc(fmtDate(j.closed_at))})">posting closed</span>`);
  return `<div class="pl-card${j.id === sel ? " active" : ""}" draggable="true" data-id="${esc(j.id)}" tabindex="0">
    <div class="t">${esc(j.title)}</div><div class="s">${esc(j.company)}</div><div class="flags">${flags.join("")}</div></div>`;
}

function renderBoard() {
  const total = board.columns.reduce((n, c) => n + c.jobs.length, 0);
  $("#pl-empty").hidden = total > 0;
  $("#pl-board").innerHTML = board.columns.map((c) => `
    <section class="pl-col" data-col="${c.key}" data-drop="${c.drop_status}" aria-label="${esc(c.label)}">
      <h3>${esc(c.label)} <span class="hint">${c.jobs.length}</span></h3>
      <div class="pl-cards">${c.jobs.map(card).join("") || '<div class="pl-drop-hint">Drop a job here</div>'}</div>
    </section>`).join("");
}

// ---------------------------------------------------------------- one job: status, follow-up, notes, timeline
async function select(id) {
  sel = id;
  $$(".pl-card").forEach((c) => c.classList.toggle("active", c.dataset.id === id));
  const j = findJob(id);
  if (!j) return closeDetail();
  let events = [];
  try { events = await deps.api(`/api/jobs/${enc(id)}/events`); } catch (e) { deps.toast(e.message); }
  renderDetail(j, events);
}

function closeDetail() {
  sel = null;
  $("#pl-detail").hidden = true;
}

function renderDetail(j, events) {
  const el = $("#pl-detail");
  el.hidden = false;
  const options = Object.entries(board.labels).filter(([s]) => s !== "new")
    .map(([s, l]) => `<option value="${s}"${s === j.status ? " selected" : ""}>${esc(l)}</option>`).join("");
  const meta = [j.company, j.location, j.req_id].filter(Boolean).map(esc).join(" · ");
  el.innerHTML = `
    <div class="pl-row"><h3>${esc(j.title)}</h3><span class="spacer"></span><button type="button" class="linkish" data-act="close" aria-label="Close details">✕</button></div>
    <div class="hint">${meta}</div>
    ${j.suggest_ghosted ? `<div class="pl-note warn">No update for ${j.days_in_status} days. <button type="button" class="linkish" data-act="ghost">Move to Ghosted</button></div>` : ""}
    ${j.closed_at ? `<div class="pl-note bad">The posting was removed from the company's site on ${esc(fmtDate(j.closed_at))}.</div>` : ""}
    <label class="pl-field">Status <select id="pl-status">${options}</select></label>
    <div class="pl-field">Follow-up
      <div class="pl-row"><input type="date" id="pl-fu-date" value="${esc(j.next_action_at || "")}">
        <input id="pl-fu-action" placeholder="e.g. Email the recruiter" value="${esc(j.next_action || "")}" maxlength="200">
        <button type="button" class="btn small" data-act="follow-up">Set</button>
        ${j.next_action_at ? '<button type="button" class="btn small" data-act="clear-follow-up">Clear</button>' : ""}</div>
    </div>
    <label class="pl-field">Note <textarea id="pl-note" maxlength="4000" placeholder="Who you spoke to, what they said, what's next…"></textarea></label>
    <div class="pl-row"><button type="button" class="btn small primary" data-act="note">Add note</button><span class="spacer"></span>
      ${j.url ? `<a class="btn small" href="${esc(j.url)}" target="_blank" rel="noopener">Posting ↗</a>` : ""}
      ${j.folder ? '<button type="button" class="btn small" data-act="folder">Folder</button>' : ""}
      <button type="button" class="btn small" data-act="show">Open in Job Agent</button></div>
    <h4>Timeline</h4>
    <ol class="pl-timeline">${events.slice().reverse().map(eventItem).join("") || "<li>No history yet.</li>"}</ol>`;
}

function eventItem(e) {
  let what;
  if (e.kind === "status") what = `${e.from_status ? `${label(e.from_status)} → ` : ""}<b>${label(e.to_status)}</b>${e.note ? `<div class="hint">${esc(e.note)}</div>` : ""}`;
  else if (e.kind === "note") what = `<div class="pl-note-text">${esc(e.note)}</div>`;
  else if (e.kind === "closed") what = `<span class="bad">${esc(e.note)}</span>`;
  else what = esc(e.note || e.kind);
  return `<li class="ev-${esc(e.kind)}"><time datetime="${esc(e.at)}">${esc(fmtWhen(e.at))}</time>${what}</li>`;
}

async function onDetailClick(e) {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act || !sel) return;
  const id = sel;
  const job = findJob(id);
  try {
    if (act === "close") return closeDetail();
    if (act === "ghost") return move(id, "ghosted");
    if (act === "show") { $("#pipeline-dialog").close(); return deps.onShowJob(id); }
    if (act === "folder") return void (await deps.api("/api/open-folder", { method: "POST", body: { path: job.folder } }));
    if (act === "note") {
      const note = $("#pl-note").value.trim();
      if (!note) return deps.toast("Write a note first.");
      await deps.api(`/api/jobs/${enc(id)}/notes`, { method: "POST", body: { note } });
    } else if (act === "follow-up" || act === "clear-follow-up") {
      const at = act === "follow-up" ? $("#pl-fu-date").value || null : null;
      if (act === "follow-up" && !at) return deps.toast("Pick a date for the follow-up.");
      await deps.api(`/api/jobs/${enc(id)}/follow-up`, { method: "PUT", body: { at, action: at ? $("#pl-fu-action").value : "" } });
    }
  } catch (err) { return deps.toast(err.message, 8000); }
  await refresh();
}
