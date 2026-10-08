// Saved searches (named keyword sets, optionally on a schedule), the run history, and new-match alerts.

import { esc } from "./resume-view.js";

const $ = (s, r = document) => r.querySelector(s);
const when = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
const EVERY = { 1: "every hour", 3: "every 3 hours", 6: "every 6 hours", 12: "every 12 hours", 24: "daily", 168: "weekly" };
const POLL_MS = 60_000;

let deps = null;     // { api, toast, getKeywords, setKeywords, industries, runSearch, selectJob, onSearchRunning }
let searches = [];
let editing = null;  // saved search being edited in the dialog (null: a new one)
const notified = new Set();

export const savedSearches = () => searches;
export const selectedSearch = () => searches.find((s) => String(s.id) === $("#saved-search").value) || null;

export async function initSearches(d) {
  deps = d;
  $("#saved-search").onchange = onPick;
  $("#btn-save-search").onclick = () => openEditor(selectedSearch());
  $("#btn-manage-searches").onclick = openManager;
  $("#ss-cancel").onclick = () => $("#search-dialog").close();
  $("#ss-save").onclick = saveEditor;
  $("#ss-delete").onclick = deleteEditing;
  $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); saveEditor(); });
  $("#sm-close").onclick = () => $("#searches-dialog").close();
  $("#sm-list").addEventListener("click", onManagerClick);
  $("#sm-runs").addEventListener("click", async (e) => {
    const del = e.target.closest("[data-delrun]");
    if (del) {
      try { await deps.api(`/api/search/runs/${del.dataset.delrun}`, { method: "DELETE" }); } catch (err) { deps.toast(err.message); }
      return renderManager();
    }
    const b = e.target.closest("[data-log]");
    if (!b) return;
    const log = $(`#run-log-${b.dataset.log}`);
    log.hidden = !log.hidden;
  });
  $("#sm-clear-runs").onclick = async () => {
    if (!confirm("Clear the whole run history? Saved searches and the jobs they found stay.")) return;
    try { await deps.api("/api/search/runs", { method: "DELETE" }); } catch (err) { deps.toast(err.message); }
    renderManager();
  };
  $("#sm-copy").onclick = () => navigator.clipboard?.writeText($("#sm-register").textContent).then(() => deps.toast("Copied."), () => deps.toast("Select the command and copy it."));
  $("#sm-notify").onclick = askNotifications;
  $("#sm-open-alerts").onclick = $("#al-folder").onclick = () => deps.api("/api/open", { method: "POST", body: { what: "alerts" } }).catch((e) => deps.toast(e.message));
  $("#chip-alerts").onclick = openAlerts;
  $("#al-close").onclick = () => $("#alerts-dialog").close();
  $("#al-seen").onclick = async () => { await deps.api("/api/alerts/seen", { method: "POST", body: { ids: null } }); $("#alerts-dialog").close(); refreshAlerts(); };
  $("#al-list").addEventListener("click", async (e) => {
    const row = e.target.closest("[data-job]");
    if (!row) return;
    await deps.api("/api/alerts/seen", { method: "POST", body: { ids: [Number(row.dataset.alert)] } });
    $("#alerts-dialog").close();
    refreshAlerts();
    deps.selectJob(row.dataset.job);
  });
  await loadSearches();
  syncPicker();
  refreshAlerts();
  setInterval(() => { if (!document.hidden) background(); }, POLL_MS);
}

// ---------------------------------------------------------------- the picker above the keyword boxes
async function loadSearches() {
  try { searches = await deps.api("/api/searches"); } catch { searches = []; }
  const sel = $("#saved-search");
  const keep = sel.value;
  sel.innerHTML = '<option value="">Current keywords</option>' + searches.map((s) =>
    `<option value="${s.id}">${esc(s.name)}${s.every_hours && s.enabled ? ` · ${EVERY[s.every_hours] || `every ${s.every_hours} h`}` : ""}</option>`).join("");
  sel.value = searches.some((s) => String(s.id) === keep) ? keep : "";
}

const norm = (s) => (s || "").split(/[,;\n]/).map((x) => x.trim().toLowerCase()).filter(Boolean).join(",");
const sameSet = (a, b) => [...(a || [])].sort().join("\n") === [...(b || [])].sort().join("\n");

/** Show the saved search whose keywords (and industries) are in the boxes, if any. */
export function syncPicker() {
  const { mandatory, optional, omit, industries } = deps.getKeywords();
  const match = searches.find((s) => norm(s.mandatory) === norm(mandatory) && norm(s.optional) === norm(optional)
    && norm(s.omit) === norm(omit) && sameSet(s.industries, industries));
  $("#saved-search").value = match ? String(match.id) : "";
}

async function onPick() {
  const s = selectedSearch();
  if (s) await deps.setKeywords(s.mandatory, s.optional, s.omit, s.industries);
}

// ---------------------------------------------------------------- save / edit dialog
function openEditor(s) {
  editing = s;
  const f = $("#search-form");
  const kw = deps.getKeywords();
  $("#ss-title").textContent = s ? `Edit “${s.name}”` : "Save search";
  f.name.value = s?.name || "";
  f.mandatory.value = s ? s.mandatory : kw.mandatory;
  f.optional.value = s ? s.optional : kw.optional;
  f.omit.value = s ? s.omit : kw.omit;
  const chosen = new Set(s ? s.industries || [] : kw.industries || []);
  $("#ss-industries").innerHTML = deps.industries().map((x) =>
    `<label><input type="checkbox" value="${esc(x.name)}"${chosen.has(x.name) ? " checked" : ""}>${esc(x.name)}</label>`).join("")
    || '<span class="hint">No company lists found.</span>';
  f.every_hours.value = s?.every_hours ? String(s.every_hours) : "";
  f.notify_min_score.value = s?.notify_min_score != null ? String(s.notify_min_score) : "";
  f.enabled.checked = s ? s.enabled : true;
  $("#ss-delete").hidden = !s;
  $("#search-dialog").showModal();
  f.name.focus();
}

async function saveEditor() {
  const f = $("#search-form");
  const body = {
    name: f.name.value.trim(), mandatory: f.mandatory.value.trim(), optional: f.optional.value.trim(), omit: f.omit.value.trim(),
    industries: [...$("#ss-industries").querySelectorAll("input:checked")].map((c) => c.value),
    every_hours: f.every_hours.value ? Number(f.every_hours.value) : null,
    notify_min_score: f.notify_min_score.value ? Number(f.notify_min_score.value) : null, enabled: f.enabled.checked,
  };
  if (!body.name) return deps.toast("Give the search a name.");
  if (!body.mandatory && !body.optional) return deps.toast("Enter at least one keyword.");
  let saved;
  try {
    saved = editing ? await deps.api(`/api/searches/${editing.id}`, { method: "PUT", body })
      : await deps.api("/api/searches", { method: "POST", body });
  } catch (e) { return deps.toast(e.message, 8000); }
  $("#search-dialog").close();
  await loadSearches();
  $("#saved-search").value = String(saved.id);
  await deps.setKeywords(saved.mandatory, saved.optional, saved.omit, saved.industries);
  deps.toast(saved.every_hours && saved.enabled
    ? `Saved “${saved.name}”. It runs ${EVERY[saved.every_hours] || `every ${saved.every_hours} hours`} while Job Agent is open.`
    : `Saved “${saved.name}”.`);
  if ($("#searches-dialog").open) renderManager();
}

async function deleteEditing() {
  if (!editing || !confirm(`Delete the saved search “${editing.name}”? Jobs it found stay.`)) return;
  try { await deps.api(`/api/searches/${editing.id}`, { method: "DELETE" }); } catch (e) { return deps.toast(e.message); }
  $("#search-dialog").close();
  await loadSearches();
  syncPicker();
  if ($("#searches-dialog").open) renderManager();
}

// ---------------------------------------------------------------- manage dialog: list, closed-app command, history
async function openManager() {
  $("#searches-dialog").showModal();
  await loadSearches();
  renderManager();
  try {
    const h = await deps.api("/api/schedule/help");
    $("#sm-how").textContent = h.how;
    $("#sm-register").textContent = h.register;
    $("#sm-note").textContent = h.note;
  } catch (e) { $("#sm-how").textContent = e.message; }
  $("#sm-notify").hidden = !("Notification" in window) || Notification.permission === "granted";
}

async function renderManager() {
  $("#sm-list").innerHTML = searches.length ? searches.map((s) => {
    const sched = s.every_hours ? (s.enabled ? `runs ${EVERY[s.every_hours] || `every ${s.every_hours} h`}` : "schedule paused") : "manual";
    const alertTxt = s.notify_min_score != null ? ` · alerts at ${s.notify_min_score}+` : "";
    const last = s.last_run_at ? ` · last run ${when(s.last_run_at)}` : " · never run";
    const next = s.next_run_at && s.enabled ? ` · next ${when(s.next_run_at)}` : "";
    return `<div class="app-row"><div><strong>${esc(s.name)}</strong>
        <div class="sub">${esc([s.mandatory && `must: ${s.mandatory}`, s.optional && `optional: ${s.optional}`, s.omit && `omit: ${s.omit}`,
          s.industries?.length && `industries: ${s.industries.join("; ")}`].filter(Boolean).join(" · "))}</div>
        <div class="sub">${esc(sched + alertTxt + last + next)}</div></div>
      <div><button type="button" class="btn small" data-run="${s.id}">Run now</button>
        <button type="button" class="btn small" data-edit="${s.id}">Edit</button>
        <button type="button" class="btn small" data-delete="${s.id}" title="Delete this saved search (the jobs it found stay)">Delete</button></div></div>`;
  }).join("") : '<p class="hint">No saved searches yet. Enter keywords above and click <b>Save…</b>.</p>';
  let runs = [];
  try { runs = await deps.api("/api/search/runs?limit=20"); } catch { /* shown empty */ }
  $("#sm-runs").innerHTML = runs.length ? runs.map((r) => `
    <div class="app-row"><div><strong>${esc(r.search_name || "Current keywords")}</strong>
      <div class="sub">${esc(when(r.started))} · ${esc(r.trigger || "")} · ${esc(r.mode || "")} · ${r.new_jobs} new, ${r.refreshed} refreshed${r.errors.length ? ` · <span class="bad">${r.errors.length} error(s)</span>` : ""}</div>
      <pre class="log" id="run-log-${r.id}" hidden>${esc([r.log_text, ...r.errors.map((e) => `ERROR ${e}`)].filter(Boolean).join("\n"))}</pre></div>
      <div><button type="button" class="btn small" data-log="${r.id}">Log</button>
        <button type="button" class="btn small" data-delrun="${r.id}" title="Remove this run from the history">Delete</button></div></div>`).join("")
    : '<p class="hint">No runs yet.</p>';
}

async function onManagerClick(e) {
  const run = e.target.closest("[data-run]");
  const edit = e.target.closest("[data-edit]");
  const del = e.target.closest("[data-delete]");
  if (edit) return openEditor(searches.find((s) => String(s.id) === edit.dataset.edit));
  if (del) {
    const s = searches.find((x) => String(x.id) === del.dataset.delete);
    if (!s || !confirm(`Delete the saved search “${s.name}”? Jobs it found stay.`)) return;
    try { await deps.api(`/api/searches/${s.id}`, { method: "DELETE" }); } catch (err) { return deps.toast(err.message); }
    await loadSearches();
    syncPicker();
    return renderManager();
  }
  if (run) {
    const s = searches.find((x) => String(x.id) === run.dataset.run);
    $("#searches-dialog").close();
    $("#saved-search").value = String(s.id);
    await deps.setKeywords(s.mandatory, s.optional, s.omit, s.industries);
    deps.runSearch(s.id);
  }
}

// ---------------------------------------------------------------- alerts + desktop notifications
async function askNotifications() {
  if (!("Notification" in window)) return deps.toast("This browser has no desktop notifications.");
  const p = await Notification.requestPermission();
  deps.toast(p === "granted" ? "Desktop notifications are on for new matches." : "Desktop notifications stay off.");
  $("#sm-notify").hidden = p === "granted";
}

async function refreshAlerts() {
  let body;
  try { body = await deps.api("/api/alerts"); } catch { return; }
  const list = body.alerts;
  const chip = $("#chip-alerts");
  chip.hidden = !list.length;
  chip.textContent = `🔔 ${list.length} new match${list.length === 1 ? "" : "es"}`;
  if ("Notification" in window && Notification.permission === "granted") {
    for (const a of list) {
      if (notified.has(a.id)) continue;
      notified.add(a.id);
      const n = new Notification(`${a.score} · ${a.title}`, { body: `${a.company} · ${a.location || ""}${a.search_name ? `\n${a.search_name}` : ""}`, tag: `jobagent-${a.id}` });
      n.onclick = () => { window.focus(); deps.selectJob(a.job_id); };
    }
  } else list.forEach((a) => notified.add(a.id));
  if ($("#alerts-dialog").open) renderAlerts(list);
  return list;
}

async function openAlerts() {
  const list = await refreshAlerts();
  renderAlerts(list || []);
  $("#alerts-dialog").showModal();
}

function renderAlerts(list) {
  $("#al-list").innerHTML = list.length ? list.map((a) => `
    <div class="app-row al-row" data-job="${esc(a.job_id)}" data-alert="${a.id}" tabindex="0" role="button">
      <div><strong>${esc(a.title)}</strong> <span class="sub">${esc(a.company)} · ${esc(a.location || "")}</span>
        <div class="sub">${esc(a.search_name || "")} · ${esc(when(a.created_at))}</div></div>
      <div class="score hi" title="Match score">${a.score}</div></div>`).join("")
    : '<p class="hint">No new matches.</p>';
}

// ---------------------------------------------------------------- every minute while the page is visible
async function background() {
  refreshAlerts();
  try {
    const st = await deps.api("/api/search/status");
    if (st.running) deps.onSearchRunning(); // a scheduled search started: show its progress
  } catch { /* offline for a moment */ }
}
