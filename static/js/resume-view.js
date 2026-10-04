// Renders a resume shaped like master_resume.yaml. In the tailored view every line the AI touched shows what
// changed (green), and hovering the line reveals Undo / Redo / "Use AI version" at its end.

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const words = (s) => (s || "").split(/(\s+)/).filter((t) => t !== "");
const bare = (w) => w.toLowerCase().replace(/^[^\w+#.]+|[^\w+#]+$/g, "");

// ---------------------------------------------------------------- data helpers
/** Split "a, b (c, d); e" on separators that are not inside brackets. */
export function splitItems(text) {
  const out = [];
  let cur = "", depth = 0;
  for (const ch of text) {
    if ("([{".includes(ch)) depth++;
    else if (")]}".includes(ch)) depth = Math.max(0, depth - 1);
    if (depth === 0 && ",;|•·".includes(ch)) { out.push(cur); cur = ""; } else cur += ch;
  }
  out.push(cur);
  return out.map((x) => x.trim()).filter(Boolean);
}
export const groupText = (g) => (g ? (g.name ? `${g.name}: ${g.items.join(", ")}` : g.items.join(", ")) : "");
export function parseGroup(text) {
  const m = text.match(/^([^:]{1,40}):\s*(.*)$/);
  return { name: m ? m[1].trim() : "", items: splitItems(m ? m[2] : text) };
}
export const textOf = (v) => (v == null ? "" : typeof v === "string" ? v : groupText(v));
export const getAt = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
export function setAt(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((o, k) => o[k], obj)[last] = value;
}
const isGroupPath = (path) => /\.groups\.\d+$/.test(path);
/** The value a typed line stands for at `path` (skills lines become {name, items}). */
export const valueFromText = (path, text) => (isGroupPath(path) ? parseGroup(text) : text);

export const ENTRY_FIELDS = { experience: ["title", "company"], education: ["degree", "school"], projects: ["name", "organization"] };
const entryDates = (e) => (e.start && e.end ? `${e.start} – ${e.end}` : e.start || e.end || "");
function entryHeading(kind, e) {
  if (e.heading?.length) return e.heading.map((l) => [l, ""]);
  const [a, b] = ENTRY_FIELDS[kind];
  const main = [e[a], e[b]].filter(Boolean).join(" — ");
  const meta = [e.location, entryDates(e)].filter(Boolean).join(" | ");
  return main || meta ? [[main, meta]] : [];
}

// ---------------------------------------------------------------- word diff
/** Mark which tokens of `next` are new compared with `prev` (LCS over words). */
function diffFlags(prev, next) {
  const a = words(prev).filter((t) => !/^\s+$/.test(t)).map(bare);
  const bTokens = words(next);
  const bIdx = bTokens.map((t, i) => (/^\s+$/.test(t) ? -1 : i)).filter((i) => i >= 0);
  const b = bIdx.map((i) => bare(bTokens[i]));
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const inserted = new Set();
  let i = 0, j = 0;
  while (j < m) {
    if (i < n && a[i] === b[j]) { i++; j++; }
    else if (i < n && dp[i + 1][j] >= dp[i][j + 1]) i++;
    else { inserted.add(bIdx[j]); j++; }
  }
  return { tokens: bTokens, inserted };
}

function markKeywords(tokens, keywords) {
  const marked = new Set();
  const seqs = (keywords || []).map((k) => k.toLowerCase().split(/\s+/).map(bare)).filter((s) => s.length && s[0]);
  const idx = tokens.map((t, i) => (/^\s+$/.test(t) ? -1 : i)).filter((i) => i >= 0);
  for (const seq of seqs) {
    for (let p = 0; p + seq.length <= idx.length; p++) {
      if (seq.every((w, k) => bare(tokens[idx[p + k]]) === w)) for (let k = 0; k < seq.length; k++) marked.add(idx[p + k]);
    }
  }
  return marked;
}

export function highlightText(text, original, approved) {
  if (original == null) return esc(text);
  const { tokens, inserted } = diffFlags(original, text);
  const marked = markKeywords(tokens, approved);
  let html = "";
  let inIns = false;
  tokens.forEach((t, i) => {
    const space = /^\s+$/.test(t);
    const ins = space ? inIns && inserted.has(i + 1) : inserted.has(i);
    if (ins && !inIns) { html += "<ins>"; inIns = true; }
    if (!ins && inIns) { html += "</ins>"; inIns = false; }
    html += marked.has(i) ? `<mark class="kw-approved">${esc(t)}</mark>` : esc(t);
  });
  if (inIns) html += "</ins>";
  return html;
}

// ---------------------------------------------------------------- rows
const control = (act, label, title) =>
  `<button type="button" class="chg-ctl" data-act="${act}" title="${esc(title)}" contenteditable="false">${esc(label)}</button>`;

/** One editable line. `value` is a string, or {name, items} for a skills line, or null for a removed line. */
function row(tag, path, value, opts) {
  const rec = opts.diff ? opts.changes?.[path] : null;
  const text = textOf(value);
  if (!text && !rec) return "";
  const ce = opts.editable && value != null ? ' contenteditable="true" spellcheck="true"' : "";
  let cls = "blk", rowCls = "row", ctl = "", body = esc(text);
  if (rec?.state === "editing") cls += " editing";
  else if (rec?.state === "alt") {
    body = highlightText(text, textOf(rec.orig), opts.approved);
    rowCls += rec.warn ? " chg warn" : " chg";
    const what = rec.orig == null ? "Remove this added line" : "Restore the wording from your master resume";
    ctl = control("undo", "↶ Undo", rec.warn ? `Check this line: ${rec.warn}.\n${what}.` : what);
  } else if (rec) { // showing the original: undone by you, or the AI version was held back by a check
    if (value == null) body = `<s>${esc(textOf(rec.alt))}</s>`;
    rowCls += rec.held ? " held" : " undone";
    ctl = rec.held
      ? control("redo", "Use AI version", `Held back: ${rec.held}.\nAI version: ${textOf(rec.alt)}`)
      : control("redo", "↷ Redo", `Put back: ${textOf(rec.alt)}`);
  }
  return `<${tag} class="${rowCls}" data-row="${esc(path)}"><span class="${cls}" data-path="${esc(path)}"${ce}>${body}</span>${ctl}</${tag}>`;
}

const list = (rows) => (rows ? `<ul>${rows}</ul>` : "");

/**
 * res: resume data. opts.diff + opts.changes: show tailoring changes; opts.editable; opts.approved (keywords).
 * Each line carries data-row/data-path so live tailoring and undo can repaint one line at a time.
 */
export function renderResume(root, res, opts = {}) {
  if (!res) { root.innerHTML = '<div class="empty">No master resume yet. Use <b>Upload master resume</b> (DOCX, PDF, TXT or MD).</div>'; return; }
  let h = res.name ? `<h1>${esc(res.name)}</h1>` : "";
  if (res.headline) h += `<p class="contact">${esc(res.headline)}</p>`;
  const c = res.contact || {};
  const line = [c.location, c.phone, c.email, ...(c.links || [])].filter(Boolean).join(" | ");
  if (line) h += `<p class="contact">${esc(line)}</p>`;
  (c.other || []).forEach((o) => { h += `<p class="contact">${esc(o)}</p>`; });
  (res.sections || []).forEach((s, si) => {
    const P = `sections.${si}`;
    let body = "";
    if (s.kind === "summary") {
      body += row("p", `${P}.text`, s.text, opts);
      body += list((s.bullets || []).map((b, k) => row("li", `${P}.bullets.${k}`, b, opts)).join(""));
    } else if (s.kind === "skills") {
      body += (s.groups || []).map((g, k) => row("p", `${P}.groups.${k}`, g, opts)).join("");
      body += (s.lines || []).map((t, k) => row("p", `${P}.lines.${k}`, t, opts)).join("");
    } else if (ENTRY_FIELDS[s.kind]) {
      (s.entries || []).forEach((e, ei) => {
        const E = `${P}.entries.${ei}`;
        entryHeading(s.kind, e).forEach(([main, meta]) => {
          body += `<p class="role"><b>${esc(main)}</b>${meta ? `${main ? " | " : ""}${esc(meta)}` : ""}</p>`;
        });
        body += row("p", `${E}.description`, e.description, opts);
        body += list((e.bullets || []).map((b, k) => row("li", `${E}.bullets.${k}`, b, opts)).join(""));
      });
    } else {
      const tag = s.style === "bullets" ? "li" : "p";
      const rows = (s.items || []).map((t, k) => row(tag, `${P}.items.${k}`, t, opts)).join("");
      body += tag === "li" ? list(rows) : rows;
    }
    if (body) h += `<h2>${esc((s.title || s.kind).toUpperCase())}</h2>${body}`;
  });
  root.innerHTML = h;
}

/** Repaint one line. Returns false when the line isn't on screen yet (caller re-renders everything). */
export function updateBlock(root, path, res, opts = {}) {
  const el = root.querySelector(`[data-row="${CSS.escape(path)}"]`);
  if (!el) return false;
  const html = row(el.tagName.toLowerCase(), path, getAt(res, path), opts);
  if (!html) { el.remove(); return true; }
  el.outerHTML = html;
  if (opts.changes?.[path]?.state === "editing") {
    root.querySelector(`[data-row="${CSS.escape(path)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  return true;
}

/** Counts for the legend above the tailored resume. */
export function changeSummary(changes) {
  const recs = Object.values(changes || {});
  return {
    applied: recs.filter((r) => r.state === "alt").length,
    check: recs.filter((r) => r.state === "alt" && r.warn).length,
    held: recs.filter((r) => r.state !== "alt" && r.held).length,
    undone: recs.filter((r) => r.state === "orig" && !r.held).length,
  };
}

const longDate = (iso) => {
  const d = iso ? new Date(`${iso}T12:00:00`) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" }) : iso || "";
};

/**
 * The cover letter (see coverletter.js) under the resume's name and contact line, then the short answers.
 * opts as for renderResume, with opts.changes = letter.changes. Lines carry data-row/data-path like the resume.
 */
export function renderLetter(root, letter, res, opts = {}) {
  if (!letter) {
    root.innerHTML = '<div class="empty">No cover letter yet. <b>Write cover letter</b> drafts one from your tailored resume, plus short answers to common application questions.</div>';
    return;
  }
  let h = res?.name ? `<h1>${esc(res.name)}</h1>` : "";
  const c = res?.contact || {};
  const line = [c.location, c.phone, c.email, ...(c.links || [])].filter(Boolean).join(" | ");
  if (line) h += `<p class="contact">${esc(line)}</p>`;
  h += `<div class="letter"><p class="lt-date">${esc(longDate(letter.date))}</p>`;
  h += row("p", "greeting", letter.greeting, opts);
  (letter.paragraphs || []).forEach((p, i) => { h += row("p", `paragraphs.${i}`, p, opts); });
  h += `<p class="lt-sign">${esc(letter.signoff)}<br>${esc(letter.name)}</p></div>`;
  if (letter.answers?.length) {
    h += "<h2>SHORT ANSWERS</h2>";
    letter.answers.forEach((a, i) => {
      h += `<div class="qa"><p class="qa-q"><b>${esc(a.question)}</b><button type="button" class="linkish qa-copy" data-copy="${i}">Copy</button></p>`
        + `${a.hint ? `<p class="hint">${esc(a.hint)}</p>` : ""}${row("p", `answers.${i}.text`, a.text, opts)}</div>`;
    });
  }
  root.innerHTML = h;
}
