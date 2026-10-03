// Builds master_resume.yaml from an uploaded resume with the local Qwen2.5-0.5B model.
//
// The server makes a rule-based draft (sections, entries, bullets, dates, email/phone/links). The model then
// reads the top of the resume and every job / education heading and splits them into fields. Bullets and the
// summary are copied word for word, never retyped by the model. Every value the model returns must appear in
// the text it came from, otherwise the rule-based guess is kept, so the model cannot invent anything.

import { esc } from "./resume-view.js";

const FORMAT = "Copy the words exactly as written. Write none when a field is missing. Reply in exactly this format and nothing else:";
const TASKS = {
  header: {
    system: `You read the top lines of a resume and copy out three fields. ${FORMAT}\nName: <the person's name>\nHeadline: <job title or tagline, or none>\nLocation: <city and state, or none>`,
    examples: [
      ["PRIYA NAIR\nSenior Product Manager\nSeattle, WA", "Name: PRIYA NAIR\nHeadline: Senior Product Manager\nLocation: Seattle, WA"],
      ["Marcus T. Lee | Denver, CO", "Name: Marcus T. Lee\nHeadline: none\nLocation: Denver, CO"],
    ],
    fields: { name: "name", headline: "headline", location: "location" },
    prefix: "Top lines:\n",
  },
  experience: {
    system: `You split one job heading from a resume into fields. ${FORMAT}\nTitle: <job title>\nCompany: <employer name>\nLocation: <city and state, Remote, or none>`,
    examples: [
      ["Acme Corp | Senior Software Engineer | Boston, MA", "Title: Senior Software Engineer\nCompany: Acme Corp\nLocation: Boston, MA"],
      ["Marketing Analyst at Globex Inc. (Remote)", "Title: Marketing Analyst\nCompany: Globex Inc.\nLocation: Remote"],
      ["Initech — Product Designer", "Title: Product Designer\nCompany: Initech\nLocation: none"],
    ],
    fields: { title: "title", company: "company", location: "location" },
    prefix: "Heading: ",
  },
  education: {
    system: `You split one education heading from a resume into fields. ${FORMAT}\nDegree: <degree and major>\nSchool: <school name>\nLocation: <city and state, or none>`,
    examples: [
      ["Master of Science in Data Science, Stanford University, Stanford, CA", "Degree: Master of Science in Data Science\nSchool: Stanford University\nLocation: Stanford, CA"],
      ["Georgia Institute of Technology | B.S. Industrial Engineering", "Degree: B.S. Industrial Engineering\nSchool: Georgia Institute of Technology\nLocation: none"],
    ],
    fields: { degree: "degree", school: "school", location: "location" },
    prefix: "Heading: ",
  },
};
const TITLE_WORDS = /\b(engineer|developer|manager|analyst|scientist|director|lead|architect|consultant|specialist|administrator|designer|intern|associate|coordinator|officer|president|head|technician|programmer|researcher|accountant|representative|advisor|assistant|supervisor|founder|executive|recruiter|principal|senior|junior)\b/i;

const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
const usable = (v, source) => !!v && !/^(none|n\/?a|unknown|not (listed|given|available)|-+)$/i.test(v.trim()) && !!norm(v) && ` ${norm(source)} `.includes(` ${norm(v)} `);

function parseFields(raw, fields) {
  const out = {};
  for (const line of raw.split("\n")) {
    const m = line.match(/^\s*([A-Za-z]+)\s*:\s*(.*?)\s*$/);
    const key = m && fields[m[1].toLowerCase()];
    if (key && !(key in out)) out[key] = m[2].replace(/^["'<]+|["'>]+$/g, "");
  }
  return out;
}

/** The model's jobs for this draft: the header, then each experience / education heading. */
export function parseTasks(draft) {
  const tasks = [];
  if (draft._header_rest) tasks.push({ type: "header", source: draft._header_rest, target: draft, label: "the top of your resume" });
  draft.sections.forEach((s) => {
    if (s.kind !== "experience" && s.kind !== "education") return;
    s.entries.forEach((e, i) => {
      if (e._rest) tasks.push({ type: s.kind, source: e._rest, target: e, label: `${s.kind === "experience" ? "job" : "education"} heading ${i + 1} of ${s.entries.length}` });
    });
  });
  return tasks;
}

function apply(task, values) {
  const t = task.target;
  const v = { ...values };
  // A job title in the company slot (and not the other way round) means the two were swapped.
  if (task.type === "experience" && v.company && TITLE_WORDS.test(v.company) && v.title && !TITLE_WORDS.test(v.title)) [v.title, v.company] = [v.company, v.title];
  // Two fields can't hold the same text ("Headline: Austin, TX" next to "Location: Austin, TX"): the field whose
  // rule-based guess agrees keeps it, the others fall back to their guesses.
  const current = (k) => (task.type === "header" && k === "location" ? t.contact.location : t[k]);
  for (const k of Object.keys(v)) {
    const same = Object.keys(v).filter((o) => v[o] && v[k] && norm(v[o]) === norm(v[k]));
    if (same.length < 2) continue;
    const keep = same.find((o) => norm(current(o)) === norm(v[o]));
    same.filter((o) => o !== keep).forEach((o) => delete v[o]);
  }
  const words = (s) => norm(s).split(" ").filter(Boolean);
  let used = 0;
  for (const [key, raw] of Object.entries(v)) {
    if (!usable(raw, task.source)) continue;
    let val = raw.trim();
    // The model sometimes cuts a value short ("Contoso" for "Contoso Retail"). When the rule-based value is the
    // same text plus words no other field claims, keep the longer one.
    const guess = task.type === "header" && key === "location" ? t.contact.location : t[key];
    if (guess && norm(guess) !== norm(val) && ` ${norm(guess)} `.includes(` ${norm(val)} `)) {
      const claimed = new Set(Object.entries(v).filter(([k, x]) => k !== key && usable(x, task.source)).flatMap(([, x]) => words(x)));
      if (words(guess).filter((w) => !words(val).includes(w)).every((w) => !claimed.has(w))) val = guess;
    }
    if (task.type === "header" && key === "location") t.contact.location = val;
    else t[key] = val;
    used++;
  }
  return used;
}

/**
 * Run the model over every task, updating `draft` in place. ui.onUpdate(task) after each token,
 * ui.onStatus(text), ui.onTokens(count). Returns { done, used } (fields filled by the model).
 */
export async function refineWithModel({ llm, draft, signal, ui }) {
  const tasks = parseTasks(draft);
  let used = 0, tokens = 0, done = 0;
  for (const [i, task] of tasks.entries()) {
    if (signal.aborted) break;
    const spec = TASKS[task.type];
    ui.onStatus(`Reading ${task.label} (${i + 1}/${tasks.length})…`, task);
    const messages = [{ role: "system", content: spec.system }];
    spec.examples.forEach(([q, a]) => messages.push({ role: "user", content: spec.prefix + q }, { role: "assistant", content: a }));
    messages.push({ role: "user", content: spec.prefix + task.source });
    let raw = "";
    const before = JSON.stringify(task.target);
    try {
      for await (const d of llm.stream(messages, { temperature: 0, max_tokens: 60 })) {
        raw += d;
        tokens++;
        ui.onTokens(tokens);
        task.live = parseFields(raw, spec.fields);
        ui.onUpdate(task);
        if (signal.aborted) { llm.interrupt(); break; }
      }
    } catch (e) {
      console.warn("model step failed:", e);
    }
    task.live = null;
    if (signal.aborted) { Object.assign(task.target, JSON.parse(before)); break; }
    used += apply(task, parseFields(raw, spec.fields));
    done++;
    ui.onUpdate(task);
  }
  return { done, total: tasks.length, used };
}

// ---------------------------------------------------------------- live YAML preview
const RESERVED = /^(true|false|null|yes|no|on|off|~)$/i;
function scalar(v) {
  if (typeof v !== "string") return String(v);
  if (v === "" || /^\s|\s$|^[-?:,[\]{}#&*!|>'"%@`]|: | #|\n/.test(v) || RESERVED.test(v) || /^[-+.]?\d/.test(v)) return JSON.stringify(v);
  return v;
}
const empty = (v) => v == null || v === "" || (Array.isArray(v) && !v.length);

/** YAML lines for the live view; lines of `active` (the part being filled) are flagged, with `live` values shown. */
export function yamlLines(root, active, live) {
  const out = [];
  const liveFor = (obj) => {
    if (!live || !active) return null;
    if (obj === active) return live;
    if (active === root && obj === root.contact && live.location) return { location: live.location };
    return null;
  };
  const emit = (obj, indent, first, hot) => {
    const fills = liveFor(obj);
    const keys = Object.keys(obj).filter((k) => !k.startsWith("_") && !(k === "style" && obj[k] === "lines")
      && (k === "title" || k === "kind" || !empty(obj[k]) || (fills && fills[k])));
    keys.forEach((k, n) => {
      const v = fills && fills[k] ? fills[k] : obj[k];
      const h = hot || (obj === active && !(obj === root && k === "sections")) || !!(fills && fills[k]);
      const pad = n === 0 && first != null ? first : " ".repeat(indent);
      if (Array.isArray(v)) {
        out.push({ text: `${pad}${k}:`, hot: h });
        v.forEach((item) => {
          if (item && typeof item === "object") emit(item, indent + 4, `${" ".repeat(indent + 2)}- `, h);
          else out.push({ text: `${" ".repeat(indent + 2)}- ${scalar(item)}`, hot: h });
        });
      } else if (v && typeof v === "object") {
        out.push({ text: `${pad}${k}:`, hot: h });
        emit(v, indent + 2, null, h);
      } else out.push({ text: `${pad}${k}: ${scalar(v)}`, hot: h });
    });
  };
  emit(root, 0, null, false);
  return out;
}

export function yamlHtml(value, active, live) {
  return yamlLines(value, active, live).map((l) => (l.hot ? `<span class="hot">${esc(l.text)}</span>` : esc(l.text))).join("\n");
}
