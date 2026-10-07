// Live, word-by-word tailoring of a COPY of master_resume.yaml for ONE job, using the local Qwen2.5-0.5B model.
//
// What changes:                        what never changes:
//  - professional summary                - name, contact, employers, titles, dates, education
//  - selected bullets                    - master_resume.yaml itself (this works on a copy)
//  - skills order; approved keywords are worked into the summary and project bullets
//
// Every AI line is checked. A line that adds facts you did not approve (a new number, a keyword you rejected or
// did not approve) is held back: your original stays, and "Use AI version" at the end of the line puts it in.
// Everything else is applied; lines that need a second look (heavy rewording, words not in your resume, a
// dropped skill) are marked amber. Every change can be undone and redone from the end of its line.

import { getAt, setAt } from "./resume-view.js";

const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const kwRe = (alias, flags = "i") => new RegExp(`(?<![A-Za-z0-9])${escRe(alias)}(?![A-Za-z0-9+#])`, flags);
export const hasAny = (aliases, text) => aliases.some((a) => kwRe(a).test(text));
const STOP = new Set("a an and the of for to in on with by at from as or is are was were be been this that their our your it its into over per via using use used across within".split(" "));
const contentWords = (s) => (s.toLowerCase().match(/[a-z][a-z0-9+#.]*/g) || []).filter((w) => !STOP.has(w) && w.length > 2);
// Standalone numbers only: digits inside a name ("K8s", "S3", "EC2", "OAuth2") are part of the name, not a claim.
const numbersIn = (s) => (s.match(/(?<![A-Za-z])\d+(?:[.,]\d+)*/g) || []).map((n) => n.replace(/,/g, ""));

const BULLET_SYSTEM = (maxWords) => `You edit resume bullet points so they match a target job.
Rules:
- Keep every fact, number, tool and employer from the original bullet.
- Do not invent new facts, numbers, tools or achievements.
- You may add ONLY the keywords listed under "Must include", and only where they fit naturally.
- Start with a strong past-tense action verb. One sentence, at most ${maxWords} words.
- Reply with the rewritten bullet only. No quotes, no labels, no explanations.`;

const BULLET_EXAMPLES = [
  { role: "user", content: "Target job: Data Engineer\nJob keywords: ETL, Python, AWS\nRole: Data Analyst at Fabrikam\nMust include: ETL\nOriginal bullet: Built pipelines that load sales data every night.\nRewritten bullet:" },
  { role: "assistant", content: "Built ETL pipelines that load sales data every night." },
  { role: "user", content: "Target job: Software Engineer\nJob keywords: Java, REST APIs, microservices\nRole: Developer at Contoso\nMust include: (none)\nOriginal bullet: wrote java services for billing team, response time down 30%\nRewritten bullet:" },
  { role: "assistant", content: "Developed Java services for the billing team, reducing response time by 30%." },
];

const SUMMARY_SYSTEM = `You rewrite the professional summary at the top of a resume for one target job.
Rules:
- Use only facts from the original summary, the candidate's roles and skills. Never invent years, employers, degrees or numbers.
- You may add ONLY the keywords listed under "Must include".
- 2 to 3 sentences, under 70 words, no pronouns.
- Reply with the new summary only. No quotes, no labels.`;

// One worked example: without it the small model tends to echo the prompt instead of writing a summary.
const SUMMARY_EXAMPLES = [
  { role: "user", content: "Target job: Backend Engineer at Initech\nJob keywords to emphasize: Java, Kubernetes, REST APIs\nMust include: Kubernetes\nCandidate's roles: Software Engineer at Globex\nCandidate's skills: Java, Spring Boot, PostgreSQL, Docker, Kubernetes\nOriginal summary: Software engineer with 5 years of experience building web services. Enjoys mentoring and clean code.\nTailored summary:" },
  { role: "assistant", content: "Software engineer with 5 years of experience building Java web services and REST APIs with Spring Boot, deployed with Docker and Kubernetes. Enjoys mentoring and writing clean code." },
];

export function cleanOutput(raw) {
  let s = (raw || "").replace(/\r/g, "");
  s = s.replace(/^\s*(here('| i)s|sure|certainly)[^\n:]*:\s*/i, "");
  s = s.replace(/^\s*(rewritten|tailored|revised|updated)?\s*(bullet|summary|version)?\s*:\s*/i, "");
  const lines = s.split("\n").map((l) => l.trim()).filter(Boolean);
  s = (lines[0] || "").replace(/^[-•*●▪\d.)\s]+(?=[A-Za-z])/, "").replace(/^["'“”]+|["'“”]+$/g, "").trim();
  return s;
}

// Loose word stem so "reduced"/"reducing" or "pipeline"/"pipelines" count as the same word.
const stem = (w) => w.replace(/(ing|ed|es|s)$/, "").slice(0, 6);
// Rewording verbs and connectors a rewrite may add without changing the claim.
const FREE = new Set(["develop", "design", "build", "built", "creat", "deliv", "led", "lead", "manag", "implem", "optimi",
  "improv", "reduc", "increa", "enabl", "drive", "drove", "own", "partne", "collab", "support", "streaml", "autom",
  "engine", "establi", "launch", "architec", "maintai", "scale", "scalab", "result", "cutting", "achiev", "spearh",
  "orches", "transf", "produc", "deploy", "experi", "years", "year", "skill", "proven", "strong", "expert", "focus",
  "team", "teams", "cross", "functi", "end", "data", "solution", "solut", "modern", "robust", "effici", "reliab"]);

/**
 * Check one AI line against the original.
 * hard: adds unapproved facts or is unusable -> held back (your original stays).
 * soft: worth a second look -> applied, marked amber.
 */
const PROMPT_LABEL = /\b(target job|job keywords|must include|original (bullet|summary)|rewritten bullet|tailored summary|candidate's (roles|skills)|role|facts|keywords to mention|question)\s*:/i;
const EXAMPLE_TEXT = [...BULLET_EXAMPLES, ...SUMMARY_EXAMPLES].filter((m) => m.role === "assistant").map((m) => m.content).join(" ");
const ngrams = (s, n) => {
  const w = s.toLowerCase().match(/[a-z0-9+#]+/g) || [];
  return w.slice(0, Math.max(0, w.length - n + 1)).map((_, i) => w.slice(i, i + n).join(" "));
};
// Names of tools, products and companies are capitalised ("Excel", "Salesforce", "GraphQL") or symbol-laden
// ("C++", "CI/CD"); a new one the resume never mentions is a new claim.
function nameTokens(s) {
  const out = [];
  for (const m of s.matchAll(/[A-Za-z0-9][\w+#./-]*/g)) {
    const tok = m[0].replace(/[.,;:/-]+$/, "");
    const before = s.slice(0, m.index).trimEnd();
    const sentenceStart = !before || /[.!?:]$/.test(before);
    if ((/[A-Z]/.test(tok) && !sentenceStart) || /[+#]|[A-Za-z]\/[A-Za-z]/.test(tok)) out.push(tok);
  }
  return out;
}

/** kind: "bullet", "summary", "letter" (a cover-letter paragraph: retells your bullets, so new words are flagged
 *  sooner) or "answer" (a short answer to an application question). Letters and answers reword freely.
 *  examples: the worked examples given to the model, so copying one is caught (tailoring's own are built in). */
export function validate(original, out, { forbidden, kind, allowed = [], extraContext = "", required = [], examples = "" }) {
  const soft = [];
  const loose = kind === "summary" || kind === "letter" || kind === "answer";
  const prose = kind === "letter" || kind === "answer";
  if (!out) return { hard: "empty output", soft };
  if (PROMPT_LABEL.test(out)) return { hard: "garbled (repeated the instructions)", soft };
  if (out.length < Math.min(25, original.length * 0.5)) return { hard: "too short", soft };
  if (out.length > original.length * 2.5 + 200) return { hard: "garbled (far too long)", soft };
  const origGrams = new Set(ngrams(original, 4));
  const seen = new Set();
  for (const g of ngrams(out, 4)) {
    if (seen.has(g) && !origGrams.has(g)) return { hard: "garbled (repeats itself)", soft };
    seen.add(g);
  }
  const exampleGrams = new Set(ngrams(`${EXAMPLE_TEXT} ${examples}`, 4));
  const ownGrams = new Set(ngrams(`${original} ${extraContext}`, 4));
  if (ngrams(out, 4).some((g) => exampleGrams.has(g) && !ownGrams.has(g))) return { hard: "garbled (copied the example)", soft };
  const origNums = new Set(numbersIn(original));
  const invented = numbersIn(out).filter((n) => !origNums.has(n));
  if (invented.length) return { hard: `adds the number ${invented[0]}, which is not in your resume`, soft };
  for (const f of forbidden) {
    if (hasAny(f.aliases, out) && !hasAny(f.aliases, original)) return { hard: `uses “${f.keyword}”, which you did not approve`, soft };
  }
  const knownText = `${original} ${allowed.join(" ")} ${extraContext}`.toLowerCase();
  const knownTight = knownText.replace(/\s+/g, "");
  const newName = nameTokens(out).find((t) => !knownText.includes(t.toLowerCase()) && !knownTight.includes(t.toLowerCase()));
  if (newName) return { hard: `adds “${newName}”, which is not in your resume`, soft };
  if (!loose && out.length > original.length * 1.8 + 80) soft.push("much longer than the original");
  // "migrated a warehouse to Snowflake" must not become "migrated Snowflake to a warehouse": a named tool that
  // followed to/into/from/on... in the original should still follow the same word.
  const PREP = /\b(to|into|from|on|onto|in|with|using|via|for)\s+(?:the\s+|a\s+|an\s+)?([A-Z][\w+#.-]*|[\w]+[+#/][\w+#/]*)/g;
  for (const [, prep, rawName] of prose ? [] : original.matchAll(PREP)) {
    const name = rawName.replace(/[.,;:-]+$/, ""); // "...to Snowflake." names Snowflake, not "Snowflake."
    if (!name) continue;
    const re = new RegExp(`\\b${prep}\\s+(?:the\\s+|a\\s+|an\\s+)?${escRe(name)}(?![\\w+#])`, "i");
    if (kwRe(name).test(out) && !re.test(out)) { soft.push(`moved “${name}” (was “${prep} ${name}”), check the meaning`); break; }
  }
  for (const r of required) if (!hasAny(r.aliases, out)) soft.push(`dropped “${r.keyword}”`);
  const ow = new Set(contentWords(original));
  if (ow.size >= 4) {
    const kept = [...ow].filter((w) => out.toLowerCase().includes(w)).length;
    if (kept / ow.size < (loose ? 0.25 : 0.35)) soft.push("reworded heavily");
  }
  // New claims: content words in neither the original, the approved keywords, nor (for the summary) the
  // candidate's roles and skills. A few rewording words are fine; more means a look is needed.
  const known = new Set([...contentWords(`${original} ${allowed.join(" ")} ${extraContext}`)].map(stem));
  const added = [...new Set(contentWords(out).map(stem))].filter((w) => !known.has(w) && !FREE.has(w));
  if (added.length > (kind === "letter" ? 4 : loose ? 10 : 2)) soft.push(`adds words not in your resume (${added.slice(0, 3).join(", ")}…)`);
  return { hard: null, soft };
}

/** Reorder each skills line so the job's keywords come first. Approved keywords are worked into the summary and
 *  project bullets instead (see tailorResume), never listed as a separate skills line. */
function orderSkills(res, changes, jdKeywords, aliases) {
  const si = res.sections.findIndex((s) => s.kind === "skills");
  if (si < 0) return;
  const isJd = (item) => jdKeywords.some((k) => hasAny(aliases[k] || [k], item));
  res.sections[si].groups.forEach((g, gi) => {
    if (!g || g.items.length < 2) return;
    const items = [...g.items.filter(isJd), ...g.items.filter((i) => !isJd(i))];
    if (items.join("\n") === g.items.join("\n")) return;
    const next = { ...g, items };
    changes[`sections.${si}.groups.${gi}`] = { orig: g, alt: next, state: "alt" };
    res.sections[si].groups[gi] = next;
  });
}

const roleOf = (kind, e) => (kind === "experience" ? [e.title, e.company].filter(Boolean).join(" at ") : e.name || "");

function pickBullets(res, jd, approvedInfo, maxBullets) {
  const bullets = [];
  const isProject = new Set();
  res.sections.forEach((s, si) => {
    if (s.kind === "summary") (s.bullets || []).forEach((b, k) => bullets.push({ path: `sections.${si}.bullets.${k}`, text: b, role: "" }));
    if (s.kind !== "experience" && s.kind !== "projects") return;
    s.entries.forEach((e, ei) => e.bullets.forEach((b, k) => {
      const path = `sections.${si}.entries.${ei}.bullets.${k}`;
      if (s.kind === "projects") isProject.add(path);
      bullets.push({ path, text: b, role: roleOf(s.kind, e) });
    }));
  });
  bullets.forEach((x, i) => { x.order = i; x.assigned = []; x.rel = jd.matched.filter((k) => hasAny(jd.aliases[k] || [k], x.text)).length; });
  // Give each approved keyword to the bullet whose wording is closest to where the JD uses it.
  // Project bullets come first; other bullets only when the resume has no project bullets.
  const pool = bullets.some((x) => isProject.has(x.path)) ? bullets.filter((x) => isProject.has(x.path)) : bullets;
  for (const kw of approvedInfo) {
    const ctx = new Set(contentWords(`${kw.context} ${kw.keyword}`));
    let best = null, bestScore = -1;
    for (const x of pool) {
      if (x.assigned.length >= 2) continue;
      const sc = contentWords(x.text).filter((w) => ctx.has(w)).length * 2 + x.rel;
      if (sc > bestScore) { best = x; bestScore = sc; }
    }
    if (best) best.assigned.push(kw.keyword);
  }
  const chosen = bullets.filter((x) => x.assigned.length);
  const rest = bullets.filter((x) => !x.assigned.length && x.rel > 0).sort((a, b) => b.rel - a.rel);
  return [...chosen, ...rest].slice(0, Math.max(maxBullets, chosen.length)).sort((a, b) => a.order - b.order);
}

function firstBulletPath(res) {
  for (const kind of ["projects", "experience"]) {
    for (const [si, s] of res.sections.entries()) {
      if (s.kind !== kind) continue;
      for (const [ei, e] of s.entries.entries()) if (e.bullets.length) return `sections.${si}.entries.${ei}.bullets.0`;
    }
  }
  return null;
}

/**
 * Tailor a copy of `master` (master_resume.yaml data) for `job`.
 * ui.onRender(res, changes) repaints everything, ui.onBlock(path) repaints one line, ui.onStatus(text), ui.onTokens(stats).
 * Returns { res, changes, stats }; changes[path] = { orig, alt, state: "alt" | "orig", held?, warn? }.
 */
const SUMMARY_KEYWORDS = 3;

export async function tailorResume({ llm, master, job, analysis, approved, maxBullets = 12, signal, ui }) {
  const res = structuredClone(master);
  const changes = {};
  const aliases = analysis.aliases || {};
  const missing = analysis.missing || [];
  const approvedSet = new Set(approved);
  const forbidden = missing.filter((m) => !approvedSet.has(m.keyword))
    .map((m) => ({ keyword: m.keyword, aliases: aliases[m.keyword] || [m.keyword] }));
  const approvedInfo = missing.filter((m) => approvedSet.has(m.keyword));
  // The summary gets the most important approved keywords (heaviest weight in the posting first); the rest go to bullets.
  const byWeight = [...approvedInfo].sort((a, b) => (b.weight || 0) - (a.weight || 0) || (b.count || 0) - (a.count || 0));
  const summaryKw = byWeight.slice(0, SUMMARY_KEYWORDS).map((m) => m.keyword);
  const bulletInfo = byWeight.slice(SUMMARY_KEYWORDS);
  const jdTop = [...analysis.matched.slice(0, 8), ...approved].slice(0, 12);
  const stats = { rewritten: 0, flagged: 0, held: 0, tokens: 0, started: performance.now(), reasons: [] };

  ui.onStatus("Ordering skills for this job…");
  orderSkills(res, changes, analysis.matched, aliases);
  ui.onRender(res, changes);

  async function rewrite(path, messages, maxTokens, kind, allowed = [], extraContext = "") {
    const original = getAt(res, path);
    changes[path] = { orig: original, alt: null, state: "editing" };
    setAt(res, path, "");
    ui.onBlock(path);
    let raw = "";
    try {
      for await (const delta of llm.stream(messages, { temperature: 0.2, max_tokens: maxTokens })) {
        raw += delta;
        stats.tokens++;
        setAt(res, path, cleanOutput(raw));
        ui.onBlock(path);
        ui.onTokens(stats);
        if (signal.aborted || /\S[^\n]*\n\s*\S/.test(raw.trim())) llm.interrupt();
        if (signal.aborted) break;
      }
    } catch (e) {
      console.warn(e);
    }
    const out = cleanOutput(raw);
    // Skills the job asks for that this line already shows should survive the rewrite.
    const required = analysis.matched.filter((k) => hasAny(aliases[k] || [k], original))
      .map((k) => ({ keyword: k, aliases: aliases[k] || [k] }));
    const { hard, soft } = signal.aborted ? { hard: "stopped", soft: [] }
      : validate(original, out, { forbidden, kind, allowed, extraContext, required });
    // Unusable output (empty, garbled) is dropped; a usable line that adds unapproved facts is held back.
    if (signal.aborted || !out || out === original || /^(empty|too short|garbled)/.test(hard || "")) {
      setAt(res, path, original);
      delete changes[path];
      if (hard && !signal.aborted) stats.reasons.push(hard);
    } else if (hard) {
      setAt(res, path, original);
      changes[path] = { orig: original, alt: out, state: "orig", held: hard };
      stats.held++;
      stats.reasons.push(hard);
    } else {
      setAt(res, path, out);
      changes[path] = { orig: original, alt: out, state: "alt", ...(soft.length ? { warn: soft.join("; ") } : {}) };
      stats.rewritten++;
      if (soft.length) stats.flagged++;
    }
    ui.onBlock(path);
  }

  const si = res.sections.findIndex((s) => s.kind === "summary" && (s.text || "").length > 40);
  if (si >= 0 && !signal.aborted) {
    ui.onStatus("Rewriting summary…");
    const skills = res.sections.filter((s) => s.kind === "skills")
      .flatMap((s) => [...s.groups.filter(Boolean).flatMap((g) => g.items), ...s.lines]).join(", ").slice(0, 500);
    const roles = res.sections.filter((s) => s.kind === "experience")
      .flatMap((s) => s.entries.map((e) => roleOf("experience", e))).filter(Boolean).slice(0, 4).join("; ");
    await rewrite(`sections.${si}.text`, [
      { role: "system", content: SUMMARY_SYSTEM },
      ...SUMMARY_EXAMPLES,
      { role: "user", content: `Target job: ${job.title} at ${job.company}\nJob keywords to emphasize: ${jdTop.join(", ") || "(none)"}\nMust include: ${summaryKw.join(", ") || "(none)"}\nCandidate's roles: ${roles || "(not listed)"}\nCandidate's skills: ${skills}\nOriginal summary: ${res.sections[si].text}\nTailored summary:` },
    ], 170, "summary", summaryKw, `${skills} ${roles} ${job.title}`);
  }

  const targets = pickBullets(res, analysis, bulletInfo, maxBullets);
  for (let i = 0; i < targets.length && !signal.aborted; i++) {
    const t = targets[i];
    ui.onStatus(`Rewriting bullet ${i + 1} of ${targets.length}…`);
    await rewrite(t.path, [
      { role: "system", content: BULLET_SYSTEM(32) },
      ...BULLET_EXAMPLES,
      { role: "user", content: `Target job: ${job.title}\nJob keywords: ${jdTop.join(", ") || "(none)"}\nRole: ${t.role || "(not listed)"}\nMust include: ${t.assigned.join(", ") || "(none)"}\nOriginal bullet: ${t.text}\nRewritten bullet:` },
    ], 90, "bullet", t.assigned);
  }

  // An approved keyword the AI did not manage to place is added to the text directly, so none is left out:
  // summary keywords to the summary, the others to a bullet (project bullets first).
  const placed = () => JSON.stringify(res.sections.filter((s) => s.kind !== "skills"));
  const unplaced = (kws) => kws.filter((k) => !hasAny(aliases[k] || [k], placed()));
  const joinList = (kws) => kws.join(", ").replace(/, ([^,]*)$/, " and $1");
  const append = (path, make) => {
    const cur = getAt(res, path);
    const next = make(cur);
    changes[path] = { orig: changes[path]?.orig ?? cur, alt: next, state: "alt" };
    setAt(res, path, next);
    ui.onBlock(path);
  };
  if (!signal.aborted) {
    const sumIdx = res.sections.findIndex((s) => s.kind === "summary" && (s.text || "").length > 0);
    const lateSummary = unplaced(summaryKw);
    if (lateSummary.length && sumIdx >= 0) {
      append(`sections.${sumIdx}.text`, (cur) => `${cur.replace(/s+$/, "")} Hands-on experience with ${joinList(lateSummary)}.`);
    }
    const lateBullets = unplaced(bulletInfo.map((m) => m.keyword).concat(sumIdx >= 0 ? [] : summaryKw));
    const path = lateBullets.length && firstBulletPath(res);
    if (path) append(path, (cur) => `${cur.replace(/[.s]+$/, "")}, using ${joinList(lateBullets)}.`);
  }

  stats.seconds = (performance.now() - stats.started) / 1000;
  return { res, changes, stats };
}
