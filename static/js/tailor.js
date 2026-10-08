// Live, word-by-word tailoring of a COPY of master_resume.yaml for ONE job, using the bundled Qwen2.5-0.5B model
// (or, when Settings > AI engine names one, a larger external model, which is shown where the posting uses each keyword).
//
// What changes:                        what never changes:
//  - professional summary                - name, contact, employers, titles, dates, education
//  - selected bullets (light rewording)  - master_resume.yaml itself (this works on a copy)
//  - skills order
//  - approved keywords get NEW sentences: a technical sentence and a separate soft-skills sentence at the end of the
//    summary, and new bullets on the projects (jobs when there are no projects) that fit them best. Existing lines
//    never have a keyword pushed into them, and a soft skill never shares a sentence with a tool.
//
// Every AI line is checked. A rewording that adds facts you did not approve (a new number, a keyword you rejected or
// did not approve) is held back: your original stays, and "Use AI version" at the end of the line puts it in. A new
// sentence that fails a check is replaced by a plain one written from a template (marked amber). Lines that need a
// second look (heavy rewording, words not in your resume, a dropped skill) are marked amber. Every change can be
// undone and redone from the end of its line.

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

// ---------------------------------------------------------------- new sentences for approved keywords
const NEW_BULLET_SYSTEM = (rich) => `You add ONE new bullet point to a project or job on a resume, to show skills the target job asks for.
Rules:
- Start with a strong past-tense action verb. One sentence, 12 to ${rich ? 36 : 30} words.
- Use every skill under "Skills to show" and no other tool, product or skill name.
- Say what each skill was used for in this work, using only what the project or job already says.${rich ? " The posting sentence shown with a skill is only a hint: do not copy it." : ""}
- Do not invent numbers, metrics, employers, clients or results. No soft skills such as communication or leadership.
- Reply with the new bullet only. No quotes, no labels, no explanations.`;

const NEW_BULLET_EXAMPLES = [
  { role: "user", content: "Target job: Data Engineer\nProject: Inventory dashboard\nWhat it already says:\n- Built a dashboard that tracks store inventory every hour.\nSkills to show: Airflow, Docker\nNew bullet:" },
  { role: "assistant", content: "Scheduled the hourly inventory loads with Airflow and packaged the dashboard services in Docker containers." },
];

const NEW_SOFT_BULLET_SYSTEM = `You add ONE new bullet point to a job on a resume, to show soft skills the target job asks for.
Rules:
- Start with a strong past-tense action verb. One sentence, 10 to 30 words.
- Use every skill under "Soft skills to show". Do not name any tool, technology, programming language or product.
- Describe how the skills showed in this job, using only what the job already says.
- Do not invent numbers, metrics, employers, clients or results.
- Reply with the new bullet only. No quotes, no labels, no explanations.`;

const NEW_SOFT_BULLET_EXAMPLES = [
  { role: "user", content: "Target job: Project Coordinator\nRole: Office Assistant at Contoso\nWhat it already says:\n- Scheduled meetings and kept the shared calendar up to date.\nSoft skills to show: Time management, Communication skills\nNew bullet:" },
  { role: "assistant", content: "Kept busy weekly schedules on track through careful time management and clear communication with every team." },
];

const NEW_TECH_SENTENCE_SYSTEM = (rich) => `You write ONE new sentence to add to the professional summary at the top of a resume, to show technical skills the target job asks for.
Rules:
- Use every skill under "Skills to show" and no other tool, product or skill name.
- Connect the skills to the candidate's existing work, using only facts from the summary so far and the candidate's roles.${rich ? " The posting sentence shown with a skill is only a hint: do not copy it." : ""}
- Do not mention soft skills such as communication, leadership or teamwork.
- No numbers, no pronouns (I, my, he, she), under 30 words. Do not start with "Expert in".
- Reply with the new sentence only. No quotes, no labels.`;

const NEW_TECH_SENTENCE_EXAMPLES = [
  { role: "user", content: "Target job: Analytics Engineer at Initech\nCandidate's roles: Data Analyst at Fabrikam\nSummary so far: Data analyst with 4 years of experience in reporting for finance teams.\nSkills to show: Tableau, SQL\nNew sentence:" },
  { role: "assistant", content: "Builds finance reporting with SQL queries and Tableau dashboards that business teams rely on." },
];

const NEW_SOFT_SENTENCE_SYSTEM = `You write ONE new sentence to add to the professional summary at the top of a resume, to show soft skills the target job asks for.
Rules:
- Use every skill under "Soft skills to show".
- Describe how the candidate works with people, using only facts from the summary so far and the candidate's roles.
- Do not name any tool, technology, programming language or product.
- No numbers, no pronouns (I, my, he, she), under 30 words.
- Reply with the new sentence only. No quotes, no labels.`;

const NEW_SOFT_SENTENCE_EXAMPLES = [
  { role: "user", content: "Target job: Product Analyst at Initech\nCandidate's roles: Business Analyst at Contoso\nSummary so far: Business analyst who turns sales data into weekly reports.\nSoft skills to show: Communication skills, Attention to detail\nNew sentence:" },
  { role: "assistant", content: "Known for clear communication with sales leaders and close attention to detail in every weekly report." },
];

/** "Kubernetes (the posting says: "deploy services on Kubernetes clusters")" for the external model's prompt. */
const withUse = (m) => {
  const use = (m.context || "").replace(/\s+/g, " ").replace(/["“”]/g, "").trim().slice(0, 180);
  return use ? `${m.keyword} (the posting says: "${use}")` : m.keyword;
};

export function cleanOutput(raw) {
  let s = (raw || "").replace(/\r/g, "");
  s = s.replace(/^\s*(here('| i)s|sure|certainly)[^\n:]*:\s*/i, "");
  s = s.replace(/^\s*(rewritten|tailored|revised|updated|new)?\s*(bullet|summary|version|sentence)?\s*:\s*/i, "");
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
  "team", "teams", "cross", "functi", "end", "data", "solution", "solut", "modern", "robust", "effici", "reliab",
  "known", "appli", "hands", "work", "workin", "clear", "close", "careful"]);

/**
 * Check one AI line against the original.
 * hard: adds unapproved facts or is unusable -> held back (your original stays).
 * soft: worth a second look -> applied, marked amber.
 */
const PROMPT_LABEL = /\b(target job|job keywords|must include|original (bullet|summary)|rewritten bullet|tailored summary|candidate's (roles|skills)|role|facts|keywords to mention|question|(soft )?skills to show|summary so far|what it already says|new (bullet|sentence)|project)\s*:/i;
const EXAMPLE_TEXT = [...BULLET_EXAMPLES, ...SUMMARY_EXAMPLES, ...NEW_BULLET_EXAMPLES, ...NEW_SOFT_BULLET_EXAMPLES,
  ...NEW_TECH_SENTENCE_EXAMPLES, ...NEW_SOFT_SENTENCE_EXAMPLES].filter((m) => m.role === "assistant").map((m) => m.content).join(" ");
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
 *  examples: the worked examples given to the model, so copying one is caught (tailoring's own are built in).
 *  forbidden: [{keyword, aliases, why?}]; `why` replaces the usual "you did not approve" reason. */
export function validate(original, out, { forbidden, kind, allowed = [], extraContext = "", required = [], examples = "", rich = false }) {
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
    if (hasAny(f.aliases, out) && !hasAny(f.aliases, original)) return { hard: f.why || `uses “${f.keyword}”, which you did not approve`, soft };
  }
  const knownText = `${original} ${allowed.join(" ")} ${extraContext}`.toLowerCase();
  const knownTight = knownText.replace(/\s+/g, "");
  const newName = nameTokens(out).find((t) => !knownText.includes(t.toLowerCase()) && !knownTight.includes(t.toLowerCase()));
  if (newName) return { hard: `adds “${newName}”, which is not in your resume`, soft };
  if (!loose && out.length > original.length * (rich ? 2.6 : 1.8) + (rich ? 140 : 80)) soft.push("much longer than the original");
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
  if (added.length > (kind === "letter" ? 4 : loose ? (rich ? 16 : 10) : (rich ? 7 : 2))) soft.push(`adds words not in your resume (${added.slice(0, 3).join(", ")}…)`);
  return { hard: null, soft };
}

const PRONOUN = /\bI\b|\b(?:me|my|we|our|he|she|his|her)\b/i;

/**
 * Check one NEW sentence (a new bullet, or a sentence added to the summary) written around approved keywords.
 * keywords: [{keyword, aliases}] it should show; forbidden: keywords it must not use (rejected ones);
 * mixed: keywords of the other kind (soft skills in a technical sentence, tools in a soft-skills sentence);
 * context: the resume text it may draw on. kind: "bullet" | "summary" | "soft".
 * hard: unusable or adds a fact -> a plain template sentence is used instead. soft: applied, marked amber.
 */
export function validateNew(out, { keywords = [], forbidden = [], mixed = [], context = "", kind = "bullet", examples = "", rich = false }) {
  const soft = [];
  if (!out) return { hard: "empty output", soft };
  if (PROMPT_LABEL.test(out)) return { hard: "garbled (repeated the instructions)", soft };
  const words = out.split(/\s+/).filter(Boolean).length;
  if (words < 6) return { hard: "too short", soft };
  if (words > (rich ? 55 : 45)) return { hard: "garbled (far too long)", soft };
  const seen = new Set();
  for (const g of ngrams(out, 4)) {
    if (seen.has(g)) return { hard: "garbled (repeats itself)", soft };
    seen.add(g);
  }
  const exampleGrams = new Set(ngrams(`${EXAMPLE_TEXT} ${examples}`, 4));
  const ownGrams = new Set(ngrams(context, 4));
  if (ngrams(out, 4).some((g) => exampleGrams.has(g) && !ownGrams.has(g))) return { hard: "garbled (copied the example)", soft };
  const knownNums = new Set(numbersIn(context));
  const invented = numbersIn(out).filter((n) => !knownNums.has(n));
  if (invented.length) return { hard: `adds the number ${invented[0]}, which is not in your resume`, soft };
  for (const f of forbidden) if (hasAny(f.aliases, out)) return { hard: `uses “${f.keyword}”, which you did not approve`, soft };
  for (const m of mixed) {
    if (hasAny(m.aliases, out)) {
      return { hard: kind === "soft" ? `names “${m.keyword}” in a soft-skills sentence` : `mixes the soft skill “${m.keyword}” into a technical sentence`, soft };
    }
  }
  const knownText = `${context} ${keywords.map((k) => k.aliases.join(" ")).join(" ")}`.toLowerCase();
  const knownTight = knownText.replace(/\s+/g, "");
  const newName = nameTokens(out).find((t) => !knownText.includes(t.toLowerCase()) && !knownTight.includes(t.toLowerCase()));
  if (newName) return { hard: `adds “${newName}”, which is not in your resume`, soft };
  const present = keywords.filter((k) => hasAny(k.aliases, out));
  if (keywords.length && !present.length) return { hard: "left out the keywords", soft };
  for (const k of keywords) if (!present.includes(k)) soft.push(`dropped “${k.keyword}”`);
  if (PRONOUN.test(out)) soft.push("uses a pronoun");
  const known = new Set(contentWords(knownText).map(stem));
  const added = [...new Set(contentWords(out).map(stem))].filter((w) => !known.has(w) && !FREE.has(w));
  if (added.length > (rich ? 12 : 8)) soft.push(`adds words not in your resume (${added.slice(0, 3).join(", ")}…)`);
  return { hard: null, soft };
}

/** Reorder each skills line so the job's keywords come first. Approved keywords get new sentences instead (see
 *  tailorResume), never a separate skills line. */
function orderSkills(res, changes, jdKeywords, aliases) {
  const si = res.sections.findIndex((s) => s.kind === "skills");
  if (si < 0) return;
  const isJd = (item) => jdKeywords.some((k) => hasAny(aliases[k] || [k], item));
  (res.sections[si].groups || []).forEach((g, gi) => {
    if (!g || g.items.length < 2) return;
    const items = [...g.items.filter(isJd), ...g.items.filter((i) => !isJd(i))];
    if (items.join("\n") === g.items.join("\n")) return;
    const next = { ...g, items };
    changes[`sections.${si}.groups.${gi}`] = { orig: g, alt: next, state: "alt" };
    res.sections[si].groups[gi] = next;
  });
}

const roleOf = (kind, e) => (kind === "experience" ? [e.title, e.company].filter(Boolean).join(" at ") : e.name || "");

/** Existing bullets worth a light rewording: the ones that already show the job's keywords, best first. */
function pickBullets(res, jd, maxBullets) {
  const bullets = [];
  res.sections.forEach((s, si) => {
    if (s.kind === "summary") (s.bullets || []).forEach((b, k) => bullets.push({ path: `sections.${si}.bullets.${k}`, text: b, role: "" }));
    if (s.kind !== "experience" && s.kind !== "projects") return;
    (s.entries || []).forEach((e, ei) => (e.bullets || []).forEach((b, k) => {
      bullets.push({ path: `sections.${si}.entries.${ei}.bullets.${k}`, text: b, role: roleOf(s.kind, e) });
    }));
  });
  bullets.forEach((x, i) => { x.order = i; x.rel = jd.matched.filter((k) => hasAny(jd.aliases[k] || [k], x.text)).length; });
  return bullets.filter((x) => x.text && x.rel > 0).sort((a, b) => b.rel - a.rel).slice(0, maxBullets)
    .sort((a, b) => a.order - b.order);
}

/** Projects (or, when the resume has none, jobs) that can take new bullets, in resume order. */
function entryPool(res) {
  const all = [];
  res.sections.forEach((s, si) => {
    if (s.kind !== "projects" && s.kind !== "experience") return;
    (s.entries || []).forEach((e, ei) => {
      if (!Array.isArray(e.bullets)) e.bullets = [];
      const text = [e.name, e.title, e.company, e.organization, e.description, ...e.bullets].filter(Boolean).join(" ");
      all.push({ si, ei, e, project: s.kind === "projects", kind: s.kind, text, kws: [] });
    });
  });
  return all.some((x) => x.project) ? all.filter((x) => x.project) : all;
}

const joinList = (kws) => kws.join(", ").replace(/, ([^,]*)$/, " and $1");
// "Critical thinking" reads as "critical thinking" mid-sentence; acronyms and names keep their case.
const midSentence = (k) => (/^[A-Z][a-z]/.test(k) ? k[0].toLowerCase() + k.slice(1) : k);
const placeOf = (x) => (x.project ? (x.e.name || "this project") : x.e.title ? `the ${x.e.title} role` : "this role");
const TEMPLATE = {
  summary: (kws) => `Hands-on experience with ${joinList(kws)}.`,
  soft: (kws) => `Known for ${joinList(kws.map(midSentence))}.`,
  bullet: (kws, x) => `Applied ${joinList(kws)} in ${placeOf(x)}.`,
  softBullet: (kws, x) => `Brought ${joinList(kws.map(midSentence))} to ${placeOf(x)}.`,
};
const chunk = (xs, n) => xs.reduce((out, x, i) => (i % n ? out[out.length - 1].push(x) : out.push([x]), out), []);

/** Why a new sentence was replaced by its template, for the amber note. */
const whyTemplate = (hard) => (/^(empty|too short|garbled)/.test(hard) ? "the AI version was unusable"
  : `the AI version ${hard.replace(/^adds/, "added").replace(/^uses/, "used").replace(/^mixes/, "mixed").replace(/^names/, "named")}`);

// Work-with-people words that read naturally next to a soft skill ("communication with stakeholders").
const PEOPLE_WORDS = new Set(["Stakeholder management", "Cross-functional"]);

const SUMMARY_KEYWORDS = 3; // technical keywords in the summary's new sentence; the rest get new bullets
const NEW_PER_ENTRY = 2;    // new bullets per project / job
const SOFT_PER_SENTENCE = 3;

/**
 * Tailor a copy of `master` (master_resume.yaml data) for `job`.
 * ui.onRender(res, changes) repaints everything, ui.onBlock(path) repaints one line, ui.onStatus(text), ui.onTokens(stats).
 * Returns { res, changes, stats }; changes[path] = { orig, alt, state: "alt" | "orig", held?, warn? }.
 * A new line has orig: null (undo removes it).
 */
export async function tailorResume({ llm, master, job, analysis, approved, maxBullets = 12, signal, ui }) {
  const res = structuredClone(master);
  const changes = {};
  const aliases = analysis.aliases || {};
  const aliasOf = (k) => aliases[k] || [k];
  const kwObj = (k) => ({ keyword: k, aliases: aliasOf(k) });
  const missing = analysis.missing || [];
  const approvedSet = new Set(approved);
  const forbidden = missing.filter((m) => !approvedSet.has(m.keyword)).map((m) => kwObj(m.keyword));
  const approvedInfo = missing.filter((m) => approvedSet.has(m.keyword));
  // Soft skills ("Critical thinking") and everything else (tools, methods) are never put in the same sentence.
  const softSet = new Set([...(analysis.soft || []), ...missing.filter((m) => m.soft).map((m) => m.keyword)]);
  const byWeight = [...approvedInfo].sort((a, b) => (b.weight || 0) - (a.weight || 0) || (b.count || 0) - (a.count || 0));
  const softKw = byWeight.filter((m) => softSet.has(m.keyword)).map((m) => m.keyword);
  const hardKw = byWeight.filter((m) => !softSet.has(m.keyword)).map((m) => m.keyword);
  const softJd = [...softSet].map(kwObj);
  const hardJd = [...new Set([...Object.keys(aliases), ...approved])].filter((k) => !softSet.has(k) && !PEOPLE_WORDS.has(k))
    .map(kwObj);
  // Approved keywords go only into new sentences: a rewording of an existing line that adds one is held back.
  const notHere = approved.map((k) => ({ ...kwObj(k), why: `adds “${k}” to an existing line (approved keywords get new sentences)` }));
  const jdTop = analysis.matched.slice(0, 12);
  const rich = llm.backend === "remote"; // an external model: the posting's own sentence for each keyword, longer lines
  const infoOf = new Map(approvedInfo.map((m) => [m.keyword, m]));
  const describe = (kws) => kws.map((k) => (rich ? withUse(infoOf.get(k) || { keyword: k }) : k)).join(rich ? "; " : ", ");
  const usesOf = (kws) => (rich ? kws.map((k) => infoOf.get(k)?.context || "").join(" ") : "");
  const stats = { rewritten: 0, added: 0, flagged: 0, held: 0, tokens: 0, started: performance.now(), reasons: [] };

  ui.onStatus("Ordering skills for this job…");
  orderSkills(res, changes, analysis.matched, aliases);
  ui.onRender(res, changes);

  async function stream(messages, maxTokens, show) {
    let raw = "";
    try {
      for await (const delta of llm.stream(messages, { temperature: 0.2, max_tokens: maxTokens })) {
        raw += delta;
        stats.tokens++;
        show(cleanOutput(raw));
        ui.onTokens(stats);
        if (signal.aborted || /\S[^\n]*\n\s*\S/.test(raw.trim())) llm.interrupt();
        if (signal.aborted) break;
      }
    } catch (e) {
      console.warn(e);
    }
    return cleanOutput(raw);
  }

  /** Lightly reword one existing line (no new keywords). */
  async function rewrite(path, messages, maxTokens, kind, extraContext = "") {
    const original = getAt(res, path);
    changes[path] = { orig: original, alt: null, state: "editing" };
    setAt(res, path, "");
    ui.onBlock(path);
    const out = await stream(messages, maxTokens, (text) => { setAt(res, path, text); ui.onBlock(path); });
    // Skills the job asks for that this line already shows should survive the rewrite.
    const required = analysis.matched.filter((k) => hasAny(aliasOf(k), original)).map(kwObj);
    const { hard, soft } = signal.aborted ? { hard: "stopped", soft: [] }
      : validate(original, out, { forbidden: [...forbidden, ...notHere], kind, allowed: [], extraContext, required, rich });
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

  /**
   * Write one new sentence around `kws`. target.append: the path of a text it is added to (the summary);
   * target.list: the path of a list it becomes a new item of (a project's bullets). When the AI sentence fails a
   * check, `template` is used instead. Returns the text placed, or "" when stopped.
   */
  async function addSentence(target, { system, examples, user, kws, mixed, kind, context, template, maxTokens }) {
    let path, base = "";
    const before = target.append ? changes[target.append] : null;
    if (target.append) {
      path = target.append;
      base = (getAt(res, path) || "").replace(/\s+$/, "");
      changes[path] = { orig: before?.orig ?? base, alt: null, state: "editing" };
    } else {
      const list = getAt(res, target.list);
      list.push("");
      path = `${target.list}.${list.length - 1}`;
      changes[path] = { orig: null, alt: null, state: "editing" };
    }
    const join = (s) => (target.append ? (s ? `${base} ${s}` : base) : s);
    ui.onBlock(path);
    const out = await stream([{ role: "system", content: system }, ...(rich ? [] : examples), { role: "user", content: user }],
      maxTokens, (s) => { setAt(res, path, join(s)); ui.onBlock(path); });
    if (signal.aborted) { // put the line back as it was
      if (target.append) {
        setAt(res, path, base);
        if (before) changes[path] = before; else delete changes[path];
      } else {
        getAt(res, target.list).pop();
        delete changes[path];
      }
      ui.onRender(res, changes);
      return "";
    }
    const keywords = kws.map(kwObj);
    const { hard, soft } = validateNew(out, { keywords, forbidden, mixed, context, kind, rich });
    let sentence = out;
    const warn = [];
    if (before?.held) warn.push(`the AI rewording of this line was held back (${before.held})`);
    else if (before?.warn) warn.push(before.warn);
    if (hard) {
      sentence = template;
      stats.reasons.push(hard);
      warn.push(`written from a template (${whyTemplate(hard)}), check the wording`);
    } else warn.push(...soft);
    const text = join(sentence);
    setAt(res, path, text);
    changes[path] = { orig: changes[path].orig, alt: text, state: "alt", ...(warn.length ? { warn: warn.join("; ") } : {}) };
    stats.added++;
    if (warn.length) stats.flagged++;
    ui.onBlock(path);
    return sentence;
  }

  const skills = res.sections.filter((s) => s.kind === "skills")
    .flatMap((s) => [...(s.groups || []).filter(Boolean).flatMap((g) => g.items), ...(s.lines || [])]).join(", ").slice(0, 500);
  const roles = res.sections.filter((s) => s.kind === "experience")
    .flatMap((s) => (s.entries || []).map((e) => roleOf("experience", e))).filter(Boolean).slice(0, 4).join("; ");

  // 1. A light rewording of the summary (no new keywords).
  const si = res.sections.findIndex((s) => s.kind === "summary" && (s.text || "").length > 40);
  if (si >= 0 && !signal.aborted) {
    ui.onStatus("Rewriting summary…");
    await rewrite(`sections.${si}.text`, [
      { role: "system", content: SUMMARY_SYSTEM },
      ...(rich ? [] : SUMMARY_EXAMPLES),
      { role: "user", content: `Target job: ${job.title} at ${job.company}\nJob keywords to emphasize: ${jdTop.join(", ") || "(none)"}\nMust include: (none)\nCandidate's roles: ${roles || "(not listed)"}\nCandidate's skills: ${skills}\nOriginal summary: ${res.sections[si].text}\nTailored summary:` },
    ], rich ? 220 : 170, "summary", `${skills} ${roles} ${job.title}`);
  }

  // Where the approved keywords go: the most important technical ones and every soft skill to the summary (each
  // kind in its own sentence), the other technical ones to new bullets on the projects that fit them best.
  const sumIdx = res.sections.findIndex((s) => s.kind === "summary" && ((s.text || "").trim() || (s.bullets || []).length));
  const summaryTarget = sumIdx < 0 ? null : (res.sections[sumIdx].text || "").trim()
    ? { append: `sections.${sumIdx}.text` } : { list: `sections.${sumIdx}.bullets` };
  const pool = entryPool(res);
  let summaryHard = summaryTarget ? hardKw.slice(0, SUMMARY_KEYWORDS) : [];
  const perBullet = rich ? 3 : 2;
  const leftover = [];
  for (const k of hardKw.slice(summaryHard.length)) {
    const ctx = new Set(contentWords(`${infoOf.get(k)?.context || ""} ${k}`));
    let best = null, bestScore = -Infinity;
    for (const x of pool) {
      if (x.kws.length >= NEW_PER_ENTRY * perBullet) continue;
      const rel = analysis.matched.filter((m) => hasAny(aliasOf(m), x.text)).length;
      const sc = contentWords(x.text).filter((w) => ctx.has(w)).length * 2 + rel - x.kws.length;
      if (sc > bestScore) { best = x; bestScore = sc; }
    }
    if (best) best.kws.push(k); else leftover.push(k);
  }
  if (summaryTarget) summaryHard = [...summaryHard, ...leftover];
  const summarySoft = summaryTarget ? softKw : [];
  // Without a summary, soft skills get a new bullet on your latest job (the first one listed).
  const latestJob = (() => {
    for (const kind of ["experience", "projects"]) {
      const i = res.sections.findIndex((s) => s.kind === kind && (s.entries || []).length);
      if (i < 0) continue;
      const e = res.sections[i].entries[0];
      if (!Array.isArray(e.bullets)) e.bullets = [];
      return { si: i, ei: 0, e, project: kind === "projects", text: [e.title, e.name, e.company, e.description, ...e.bullets].filter(Boolean).join(" ") };
    }
    return null;
  })();
  const summaryNow = () => (summaryTarget?.append ? getAt(res, summaryTarget.append) || ""
    : (res.sections[sumIdx]?.bullets || []).filter(Boolean).join(" "));

  // 2. New summary sentences: one technical, one (or more) for soft skills.
  if (summaryTarget && summaryHard.length && !signal.aborted) {
    ui.onStatus("Writing a summary sentence for the technical keywords…");
    const kws = summaryHard;
    const context = `${summaryNow()} ${roles} ${skills} ${job.title} ${usesOf(kws)}`;
    await addSentence(summaryTarget, {
      system: NEW_TECH_SENTENCE_SYSTEM(rich), examples: NEW_TECH_SENTENCE_EXAMPLES, kws, mixed: softJd, kind: "summary", context,
      user: `Target job: ${job.title} at ${job.company}\nCandidate's roles: ${roles || "(not listed)"}\nSummary so far: ${summaryNow() || "(none)"}\nSkills to show: ${describe(kws)}\nNew sentence:`,
      template: TEMPLATE.summary(kws), maxTokens: rich ? 120 : 70,
    });
  }
  for (const kws of chunk(summarySoft, SOFT_PER_SENTENCE)) {
    if (signal.aborted) break;
    ui.onStatus("Writing a summary sentence for the soft skills…");
    await addSentence(summaryTarget, {
      system: NEW_SOFT_SENTENCE_SYSTEM, examples: NEW_SOFT_SENTENCE_EXAMPLES, kws, mixed: hardJd, kind: "soft", context: `${summaryNow()} ${roles}`,
      user: `Target job: ${job.title} at ${job.company}\nCandidate's roles: ${roles || "(not listed)"}\nSummary so far: ${summaryNow() || "(none)"}\nSoft skills to show: ${kws.join(", ")}\nNew sentence:`,
      template: TEMPLATE.soft(kws), maxTokens: rich ? 100 : 60,
    });
  }

  // 3. A light rewording of the existing bullets that already show the job's keywords (no new keywords).
  const targets = pickBullets(res, analysis, maxBullets);
  for (let i = 0; i < targets.length && !signal.aborted; i++) {
    const t = targets[i];
    ui.onStatus(`Rewriting bullet ${i + 1} of ${targets.length}…`);
    await rewrite(t.path, [
      { role: "system", content: BULLET_SYSTEM(rich ? 42 : 32) },
      ...(rich ? [] : BULLET_EXAMPLES),
      { role: "user", content: `Target job: ${job.title}\nJob keywords: ${jdTop.join(", ") || "(none)"}\nRole: ${t.role || "(not listed)"}\nMust include: (none)\nOriginal bullet: ${t.text}\nRewritten bullet:` },
    ], rich ? 150 : 90, "bullet");
  }

  // 4. New bullets: each project gets up to NEW_PER_ENTRY new bullets for the keywords given to it.
  const newBullets = pool.filter((x) => x.kws.length).flatMap((x) => chunk(x.kws, perBullet).map((kws) => ({ x, kws, soft: false })));
  if (!summaryTarget && softKw.length && latestJob) {
    chunk(softKw, SOFT_PER_SENTENCE).forEach((kws) => newBullets.push({ x: latestJob, kws, soft: true }));
  }
  for (let i = 0; i < newBullets.length && !signal.aborted; i++) {
    const { x, kws, soft } = newBullets[i];
    ui.onStatus(`Writing new bullet ${i + 1} of ${newBullets.length}…`);
    const label = x.project ? `Project: ${[x.e.name, x.e.organization].filter(Boolean).join(" — ") || "(not named)"}`
      : `Role: ${roleOf("experience", x.e) || "(not listed)"}`;
    const says = [x.e.description, ...x.e.bullets.slice(0, 4)].filter(Boolean).map((b) => `- ${b}`).join("\n") || "(nothing yet)";
    await addSentence({ list: `sections.${x.si}.entries.${x.ei}.bullets` }, soft ? {
      system: NEW_SOFT_BULLET_SYSTEM, examples: NEW_SOFT_BULLET_EXAMPLES, kws, mixed: hardJd, kind: "soft", context: x.text,
      user: `Target job: ${job.title}\n${label}\nWhat it already says:\n${says}\nSoft skills to show: ${kws.join(", ")}\nNew bullet:`,
      template: TEMPLATE.softBullet(kws, x), maxTokens: rich ? 100 : 70,
    } : {
      system: NEW_BULLET_SYSTEM(rich), examples: NEW_BULLET_EXAMPLES, kws, mixed: softJd, kind: "bullet",
      context: `${x.text} ${job.title} ${usesOf(kws)}`,
      user: `Target job: ${job.title}\n${label}\nWhat it already says:\n${says}\nSkills to show: ${describe(kws)}\nNew bullet:`,
      template: TEMPLATE.bullet(kws, x), maxTokens: rich ? 120 : 80,
    });
  }

  // 5. A keyword an AI sentence left out gets a plain sentence of its own (never pushed into an existing line).
  if (!signal.aborted) {
    const placed = () => JSON.stringify(res.sections.filter((s) => s.kind !== "skills"));
    const unplaced = (kws) => kws.filter((k) => !hasAny(aliasOf(k), placed()));
    const add = (target, text) => {
      if (target.append) {
        const path = target.append;
        const cur = (getAt(res, path) || "").replace(/\s+$/, "");
        const rec = changes[path];
        changes[path] = { orig: rec?.orig ?? cur, alt: `${cur} ${text}`, state: "alt", warn: [rec?.warn, "written from a template, check the wording"].filter(Boolean).join("; ") };
        setAt(res, path, `${cur} ${text}`);
      } else {
        const list = getAt(res, target.list);
        list.push(text);
        changes[`${target.list}.${list.length - 1}`] = { orig: null, alt: text, state: "alt", warn: "written from a template, check the wording" };
      }
      stats.added++;
      stats.flagged++;
    };
    const lateSoft = unplaced(softKw);
    const lateHard = unplaced(hardKw);
    if (summaryTarget) {
      const sumLate = lateHard.filter((k) => summaryHard.includes(k));
      if (sumLate.length) add(summaryTarget, TEMPLATE.summary(sumLate));
      for (const kws of chunk(lateSoft, SOFT_PER_SENTENCE)) add(summaryTarget, TEMPLATE.soft(kws));
    } else if (latestJob) {
      for (const kws of chunk(lateSoft, SOFT_PER_SENTENCE)) add({ list: `sections.${latestJob.si}.entries.${latestJob.ei}.bullets` }, TEMPLATE.softBullet(kws, latestJob));
    }
    for (const x of pool) {
      const late = lateHard.filter((k) => x.kws.includes(k));
      for (const kws of chunk(late, perBullet)) add({ list: `sections.${x.si}.entries.${x.ei}.bullets` }, TEMPLATE.bullet(kws, x));
    }
    ui.onRender(res, changes);
  }

  stats.seconds = (performance.now() - stats.started) / 1000;
  return { res, changes, stats };
}
