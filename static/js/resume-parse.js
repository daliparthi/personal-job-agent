// Builds master_resume.yaml from an uploaded resume with the local Qwen2.5-0.5B model (or the external engine chosen
// in Settings).
//
// The server makes a rule-based draft (sections, entries, bullets, dates, email/phone/links). The model then reads
// the top of the resume and splits it into name / headline / location, and writes every section (a long one, job by
// job) as loose YAML: the usual keys where they fit, and any other labelled line ("Technologies: ...") as a key of
// its own. The server uses that YAML only when it finds every value in that part of the resume (a small typo is put
// back to your own wording) and almost nothing is left out; otherwise the rule-based draft of that part stays and the
// model only splits its job / education headings into fields. Either way the model cannot invent anything.

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
// ---------------------------------------------------------------- loose YAML of a whole section or entry
const ENTRY_KINDS = new Set(["experience", "education", "projects"]);
const SECTION_LIMIT = 1800; // characters: a longer job / school / project section is read one entry at a time
const OTHER_LIMIT = 3600;   // a longer section of any other kind keeps the rule-based parse
const PART_NAMES = { summary: "a professional summary", skills: "a skills section", experience: "a work experience section",
  projects: "a projects section", education: "an education section", certifications: "a certifications section", other: "a resume section" };
const ENTRY_NAMES = { experience: "one job", education: "one school", projects: "one project" };

const LOOSE_SYSTEM = `You turn one part of a resume into YAML. Copy every word exactly as written: never add, reword, shorten or leave out anything.
Use these keys when they fit:
- a job: title, company, location, start, end, description, bullets
- a school: degree, school, location, start, end, bullets
- a project: name, organization, start, end, description, bullets
- skills: groups, each with a name and its items, when the resume names its groups; otherwise items
- certifications, awards and other lists: items, one per line
- a summary: text
Any other labelled line (for example "Technologies: ..." or "GPA: ...") becomes a key of its own, named by its label.
Jobs, schools and projects go in a list under entries.
Reply with the YAML only.`;

const LOOSE_EXAMPLES = [
  ["Part: a work experience section\nText:\nData Analyst, Fabrikam Health | Houston, TX | 2016 - 2018\n• Wrote SQL reports used by 12 clinic managers.\nTechnologies: SQL, Tableau\nIntern, Contoso | 2015\n• Cleaned claims data.",
    "entries:\n  - title: Data Analyst\n    company: Fabrikam Health\n    location: Houston, TX\n    start: 2016\n    end: 2018\n    bullets:\n      - Wrote SQL reports used by 12 clinic managers.\n    Technologies: SQL, Tableau\n  - title: Intern\n    company: Contoso\n    end: 2015\n    bullets:\n      - Cleaned claims data."],
  ["Part: a skills section\nText:\nLanguages: Python, SQL\nCloud: AWS (S3, EMR), Docker",
    "groups:\n  - name: Languages\n    items: Python, SQL\n  - name: Cloud\n    items: AWS (S3, EMR), Docker"],
  ["Part: a certifications section\nText:\nAWS Certified Developer – Associate, SnowPro Core Certification",
    "items:\n  - AWS Certified Developer – Associate\n  - SnowPro Core Certification"],
];

const entryLines = (e) => [...(e._src || []), e.description, ...(e.bullets || []).map((b) => `• ${b}`)].filter(Boolean);

function looseMessages(task) {
  const part = task.scope === "entry" ? ENTRY_NAMES[task.kind] : PART_NAMES[task.kind] || PART_NAMES.other;
  const messages = [{ role: "system", content: LOOSE_SYSTEM }];
  LOOSE_EXAMPLES.forEach(([q, a]) => messages.push({ role: "user", content: q }, { role: "assistant", content: a }));
  messages.push({ role: "user", content: `Part: ${part}\nText:\n${task.source.join("\n")}` });
  return messages;
}

/** The YAML a part needs: about one token per 2.6 characters, plus room for the keys. */
const looseTokens = (task, remote) => Math.min(remote ? 3000 : 1100, Math.ceil(task.source.join("\n").length / 2.6) + 80);

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

/** The heading-split jobs of a section (all its job / education headings, or only `only`'s). */
function headingTasks(s, only = null) {
  if (s.kind !== "experience" && s.kind !== "education") return [];
  return (s.entries || []).map((e, i) => ({ e, i })).filter(({ e }) => e._rest && (!only || e === only))
    .map(({ e, i }) => ({ type: s.kind, source: e._rest, target: e, label: `${s.kind === "experience" ? "job" : "education"} heading ${i + 1} of ${s.entries.length}` }));
}

/** The model's jobs for this draft: the header, then each section (a long job / school / project section entry by
 *  entry) as loose YAML. A section with no source lines (or a very long non-entry one) keeps the rule-based parse. */
export function parseTasks(draft) {
  const tasks = [];
  if (draft._header_rest) tasks.push({ type: "header", source: draft._header_rest, target: draft, label: "the top of your resume" });
  draft.sections.forEach((s) => {
    const lines = s._lines || [];
    const size = lines.join("\n").length;
    if (!lines.length) return;
    const name = `“${s.title || s.kind}”`;
    if (ENTRY_KINDS.has(s.kind) && size > SECTION_LIMIT && (s.entries || []).length) {
      s.entries.forEach((e, i) => tasks.push({ type: "loose", scope: "entry", kind: s.kind, title: s.title, section: s, index: i,
        source: entryLines(e), src: e._src || [], target: e, label: `${ENTRY_NAMES[s.kind]} (${i + 1} of ${s.entries.length}) in ${name}` }));
    } else if (ENTRY_KINDS.has(s.kind) || size <= OTHER_LIMIT) {
      tasks.push({ type: "loose", scope: "section", kind: s.kind, title: s.title, section: s, source: lines, src: [], target: s, label: name });
    }
  });
  return tasks;
}

/** Put a part the server accepted in place of the draft's. */
function place(task, part) {
  if (task.scope === "entry") {
    task.section.entries[task.index] = part;
    task.target = part;
    return;
  }
  const s = task.section;
  Object.keys(s).filter((k) => !k.startsWith("_")).forEach((k) => delete s[k]);
  Object.assign(s, part);
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

function headingMessages(task) {
  const spec = TASKS[task.type];
  const messages = [{ role: "system", content: spec.system }];
  spec.examples.forEach(([q, a]) => messages.push({ role: "user", content: spec.prefix + q }, { role: "assistant", content: a }));
  messages.push({ role: "user", content: spec.prefix + task.source });
  return messages;
}

/**
 * Run the model over every task, updating `draft` in place. ui.onUpdate(task) after each token,
 * ui.onStatus(text), ui.onTokens(count). check(body) asks the server whether a part's YAML can be used
 * (POST /api/resume/loose). Returns { done, total, used (heading fields filled), parts (sections / entries
 * written as YAML), kept (why a part kept the rule-based parse) }.
 */
export async function refineWithModel({ llm, draft, signal, ui, check }) {
  const tasks = parseTasks(draft);
  const remote = llm.backend === "remote";
  let used = 0, tokens = 0, done = 0, parts = 0;
  const kept = [];
  for (let i = 0; i < tasks.length; i++) {
    const task = tasks[i];
    if (signal.aborted) break;
    const loose = task.type === "loose";
    ui.onStatus(`Reading ${task.label} (${i + 1}/${tasks.length})…`, task);
    let raw = "";
    const before = JSON.stringify(task.target);
    try {
      const opts = { temperature: 0, max_tokens: loose ? looseTokens(task, remote) : 60 };
      for await (const d of llm.stream(loose ? looseMessages(task) : headingMessages(task), opts)) {
        raw += d;
        tokens++;
        ui.onTokens(tokens);
        task.live = loose ? raw : parseFields(raw, TASKS[task.type].fields);
        ui.onUpdate(task);
        if (signal.aborted) { llm.interrupt(); break; }
      }
    } catch (e) {
      console.warn("model step failed:", e);
    }
    task.live = null;
    if (signal.aborted) { if (!loose) Object.assign(task.target, JSON.parse(before)); break; }
    done++;
    if (loose) {
      let r;
      try {
        r = await check({ kind: task.kind, title: task.title, scope: task.scope, source: task.source, src: task.src, yaml: raw });
      } catch (e) {
        r = { ok: false, reason: String(e.message || e) };
      }
      if (r.ok) {
        place(task, r.part);
        parts++;
      } else {
        kept.push(`${task.label}: ${r.reason}`);
        // That part keeps the rule-based draft; its job / education headings are still split into fields.
        tasks.splice(i + 1, 0, ...headingTasks(task.section, task.scope === "entry" ? task.target : null));
      }
    } else used += apply(task, parseFields(raw, TASKS[task.type].fields));
    ui.onUpdate(task);
  }
  return { done, total: tasks.length, used, parts, kept };
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
    if (obj === active && typeof live === "string") { // the YAML the model is writing for this part, as it streams
      const head = "kind" in obj && "title" in obj ? [`title: ${scalar(obj.title)}`, `kind: ${obj.kind}`] : [];
      const raw = live.replace(/\r/g, "").replace(/^\s*```[A-Za-z]*[ \t]*\n?/, "").split("\n");
      [...head, ...(raw.some((l) => l.trim()) ? raw : ["…"])].forEach((l, n) => {
        out.push({ text: `${n === 0 && first != null ? first : " ".repeat(indent)}${l}`, hot: true });
      });
      return;
    }
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
