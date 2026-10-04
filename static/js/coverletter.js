// A cover letter and short-answer drafts for ONE job, written from your (tailored) resume with the local
// Qwen2.5-0.5B model.
//
// The opening and closing are fixed templates. Every body paragraph and short answer starts as a plain version
// stitched together from your own resume lines; the model then rewrites it. Its version is used only when it passes
// the same checks as a tailored resume line (validate() in tailor.js): no new numbers, tools, employers, or keywords
// you did not approve. Otherwise the plain version stays and "Use AI version" at the end of the line puts the AI one
// in. Every line can be undone, redone or edited, as in the tailored resume.
//
// letter = { greeting, paragraphs: [text], signoff, name, date, answers: [{ key, question, hint?, text }],
//            changes: { "paragraphs.1": { orig, alt, state, held?, warn? }, "answers.0.text": {...} } }

import { getAt, setAt } from "./resume-view.js";
import { hasAny, validate } from "./tailor.js";

// ---------------------------------------------------------------- text helpers
const firstSentence = (t) => ((t || "").trim().match(/^.*?[.!?](?=\s|$)/) || [(t || "").trim()])[0].trim();
const lcFirst = (s) => (/^[A-Z][a-z]/.test(s) ? s[0].toLowerCase() + s.slice(1) : s);
const endDot = (s) => (/[.!?]$/.test(s.trim()) ? s.trim() : `${s.trim()}.`);
const bare = (s) => s.trim().replace(/[.;,]+$/, "");
export const joinList = (xs) => (xs.length < 3 ? xs.join(" and ") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`);

// Past-tense action verbs that don't end in -ed.
const IRREGULAR = new Set(("led built ran won grew drove wrote made cut set took brought began taught sold kept found met "
  + "rebuilt rewrote overhauled spun stood shipped held oversaw").split(" "));
const isVerb = (w) => /^[A-Za-z]+ed$/.test(w || "") || IRREGULAR.has((w || "").toLowerCase());

/** "Data engineer with 8 years of ..." -> "I am a data engineer with 8 years of ...", or "" when it doesn't fit. */
export function selfIntro(summary) {
  const s = firstSentence(summary);
  if (!s || s.length > 220) return "";
  if (/^(I|I'm|My)\b/.test(s)) return endDot(s);
  const m = s.match(/^((?:[A-Za-z][\w/&+.-]*\s+){1,6}?)(with|who|experienced|specializing|focused|skilled|passionate)\b/);
  if (!m) return "";
  const body = lcFirst(s);
  return endDot(`I am ${/^[aeiou]/i.test(body) ? "an" : "a"} ${body}`);
}

// ---------------------------------------------------------------- the plain (template) versions
export function opening(job, resume) {
  const summary = (resume.sections || []).find((s) => s.kind === "summary");
  const me = selfIntro(summary?.text || "");
  return `I am writing to apply for the ${job.title} position at ${job.company}.${me ? ` ${me}` : ""}`;
}

export function closing(job, skills) {
  const top = skills.slice(0, 3);
  const how = top.length ? `how my experience with ${joinList(top)} could help ${job.company}` : `how I could help ${job.company}`;
  return `I would welcome the chance to discuss ${how}. Thank you for your time and consideration.`;
}

/**
 * The resume entries (jobs, projects) whose bullets best show what the posting asks for: up to two entries, each
 * with its two most relevant bullets. Earlier (more recent) entries win ties.
 */
export function pickEvidence(resume, analysis, approved = []) {
  const aliases = analysis.aliases || {};
  const keywords = [...(analysis.matched || []), ...approved];
  const entries = [];
  (resume.sections || []).forEach((s) => {
    if (s.kind !== "experience" && s.kind !== "projects") return;
    (s.entries || []).forEach((entry) => {
      const bullets = (entry.bullets || []).filter((b) => b && b.trim())
        .map((text, k) => ({ text, k, rel: keywords.filter((kw) => hasAny(aliases[kw] || [kw], text)).length }));
      if (bullets.length) entries.push({ kind: s.kind, entry, bullets, order: entries.length });
    });
  });
  for (const x of entries) {
    x.top = [...x.bullets].sort((a, b) => b.rel - a.rel || a.k - b.k).slice(0, 2).sort((a, b) => a.k - b.k);
    x.score = x.top.reduce((n, b) => n + b.rel, 0);
  }
  const ranked = [...entries].sort((a, b) => b.score - a.score || a.order - b.order);
  return ranked.slice(0, 2).filter((x, i) => i === 0 || x.score > 0);
}

const roleOf = ({ kind, entry }) => (kind === "experience"
  ? [entry.title, entry.company].filter(Boolean).join(" at ")
  : [entry.name, entry.organization].filter(Boolean).join(" at "));

function leadIn({ kind, entry }) {
  if (kind === "projects") return entry.name ? `On the ${entry.name} project${entry.organization ? ` at ${entry.organization}` : ""}, ` : "";
  if (entry.title && entry.company) return `As ${entry.title} at ${entry.company}, `;
  if (entry.company) return `At ${entry.company}, `;
  return entry.title ? `As ${entry.title}, ` : "";
}

/** One paragraph made only of the entry's own bullets: "As X at Y, I designed .... I also migrated ...". */
export function bodyTemplate(ev) {
  const [b1, b2] = ev.top.map((b) => bare(b.text));
  const lead = leadIn(ev);
  const first = isVerb(b1.split(/\s+/)[0]) ? `${lead}I ${lcFirst(b1)}.` : `${lead ? lead.replace(/, $/, " — ") : ""}${b1}.`;
  if (!b2) return first;
  return `${first} ${isVerb(b2.split(/\s+/)[0]) ? `I also ${lcFirst(b2)}` : b2}.`;
}

// ---------------------------------------------------------------- prompts
const LETTER_SYSTEM = `You write one body paragraph of a cover letter, in the first person ("I").
Rules:
- Use only the facts given. Keep their numbers and tool names exactly. Add no new numbers, tools, employers or achievements.
- 2 to 3 sentences, under 80 words. Past tense for past work.
- Reply with the paragraph only. No greeting, no sign-off, no labels.`;

const LETTER_EXAMPLES = [
  { role: "user", content: "Target job: Backend Engineer at Initech\nRole: Software Engineer at Globex\nFacts: As Software Engineer at Globex, I developed Java services for the billing team, reducing response time by 30%. I also built REST APIs used by 12 internal teams.\nKeywords to mention: REST APIs, Java\nParagraph:" },
  { role: "assistant", content: "As a Software Engineer at Globex, I developed Java services for the billing team and cut their response time by 30%. I also built REST APIs that 12 internal teams rely on, experience I would bring to backend work at Initech." },
];

const ANSWER_SYSTEM = `You draft a short answer to a job application question, in the first person ("I").
Rules:
- Use only the facts given. Add no numbers, tools, employers or achievements that are not in them.
- "The job" describes the employer's job, not the candidate. Never claim it as the candidate's experience.
- 2 to 3 sentences, under 70 words, as one paragraph (no lists).
- Reply with the answer only. No labels.`;

const ANSWER_EXAMPLES = [
  { role: "user", content: "Question: Why are you interested in the Backend Engineer role?\nTarget job: Backend Engineer at Initech\nFacts: The job: design REST APIs; keep services reliable. My skills that match: Java, REST APIs. My experience: As Software Engineer at Globex, I developed Java services for the billing team.\nAnswer:" },
  { role: "assistant", content: "The Backend Engineer role at Initech centers on designing REST APIs and keeping services reliable, which is the work I know best. As a Software Engineer at Globex, I developed Java services for the billing team." },
];

const EXAMPLE_TEXT = [...LETTER_EXAMPLES, ...ANSWER_EXAMPLES].filter((m) => m.role === "assistant").map((m) => m.content).join(" ");

// Greetings, sign-offs and placeholders the model sometimes adds around the paragraph.
function skipLine(line) {
  if (/^(dear\b|to whom|hello\b|hi\b|sincerely|best( regards)?\b|kind regards|warm regards|regards\b|yours\b|subject:|\[|\()/i.test(line)) return true;
  return /^thank you\b/i.test(line) && line.length < 80;
}

const LIST_ITEM = /^(?:[-•*●▪]|\d+[.)])\s+/;
const contentLines = (s) => s.split("\n").map((l) => l.trim()).filter((l) => l && !skipLine(l));

/**
 * The paragraph out of the model's raw output: no labels, quotes, greeting or sign-off. A reply that starts a list
 * ("... includes:" then "- a", "- b") becomes one sentence; a trailing unfinished sentence is dropped.
 */
export function cleanParagraph(raw) {
  let s = (raw || "").replace(/\r/g, "");
  s = s.replace(/^\s*(here('| i)s|sure|certainly)[^\n:]*:\s*/i, "");
  s = s.replace(/^\s*(paragraph|answer|body( paragraph)?|cover letter( paragraph)?)\s*:\s*/i, "");
  const lines = contentLines(s);
  let out = lines[0] || "";
  if (out.endsWith(":")) {
    const items = [];
    for (const l of lines.slice(1)) {
      if (!LIST_ITEM.test(l)) break;
      items.push(l.replace(LIST_ITEM, "").replace(/[.;,]+$/, ""));
    }
    if (items.length) out = `${out.slice(0, -1)} ${joinList(items)}.`;
  }
  return wholeSentences(out.replace(/^["'“”]+|["'“”]+$/g, "").trim());
}

/** Text up to its last complete sentence ("" when there is none). */
export function wholeSentences(s) {
  if (/[.!?]["”')]?$/.test(s)) return s;
  const ends = [...s.matchAll(/[.!?]["”')]?(?=\s)/g)];
  return ends.length ? s.slice(0, ends.at(-1).index + ends.at(-1)[0].length) : "";
}

/** Enough of the reply for one paragraph (a list after "...:" counts as part of it). */
function paragraphDone(raw) {
  const lines = contentLines(raw || "");
  if (lines.length < 2) return false;
  if (!lines[0].endsWith(":")) return true;
  const rest = lines.slice(1);
  return rest.some((l) => !LIST_ITEM.test(l)) || rest.length > 5;
}

// ---------------------------------------------------------------- plan: the letter's plain version + what the AI rewrites
const today = () => new Date().toISOString().slice(0, 10);

/**
 * The letter with its plain versions, and the rewrites to ask the model for (in order).
 * job: { title, company }; analysis: the job's match analysis ({ matched, aliases }); digest: { about, duties }
 * (the posting's own sentences about the company and the role).
 */
export function planLetter({ resume, job, analysis, approved = [], digest = {} }) {
  const skills = (analysis.matched || []).slice(0, 6);
  const evidence = pickEvidence(resume, analysis, approved);
  const bodies = evidence.map(bodyTemplate);
  const about = (digest.about || [])[0];
  const duties = (digest.duties || []).slice(0, 3);
  const recent = evidence[0];
  const recentLine = recent ? bodyTemplate({ ...recent, top: recent.top.slice(0, 1) }) : "";
  const skillsWith = skills.length ? ` with ${joinList(skills.slice(0, 3))}` : "";

  const answers = [
    { key: "why_role", question: `Why are you interested in the ${job.title} role?`,
      text: `The ${job.title} role lines up with the work I have done${skillsWith}.${recentLine ? ` ${recentLine}` : ""}` },
    { key: "why_company", question: `Why do you want to work at ${job.company}?`,
      hint: "Make this one personal: say in your own words why you want to work there.",
      text: about
        ? `What draws me to ${job.company} is how it describes its work: “${about}” I would like to bring my experience${skillsWith} to that work.`
        : `I would like to bring my experience${skillsWith} to ${job.company}, and the ${job.title} role is a strong match for it.` },
    { key: "experience", question: "What relevant experience do you have for this role?",
      text: bodies[0] || `My experience${skillsWith} matches what the ${job.title} role asks for.` },
  ];
  const letter = {
    greeting: `Dear ${job.company} hiring team,`,
    paragraphs: [opening(job, resume), ...bodies, closing(job, skills)],
    signoff: "Sincerely,", name: resume.name || "", date: today(), answers, changes: {},
  };

  const target = `Target job: ${job.title} at ${job.company}`;
  const known = `${job.title} ${job.company} ${skills.join(" ")} ${approved.join(" ")}`;
  const tasks = evidence.map((ev, i) => ({
    path: `paragraphs.${i + 1}`, kind: "letter", status: `Writing paragraph ${i + 1} of ${evidence.length}…`, maxTokens: 150,
    context: `${known} ${roleOf(ev)}`,
    messages: [{ role: "system", content: LETTER_SYSTEM }, ...LETTER_EXAMPLES,
      { role: "user", content: `${target}\nRole: ${roleOf(ev) || "(not listed)"}\nFacts: ${bodies[i]}\nKeywords to mention: ${skills.slice(0, 4).join(", ") || "(none)"}\nParagraph:` }],
  }));
  const facts = {
    why_role: `The job: ${duties.join("; ") || job.title}. My skills that match: ${skills.join(", ") || "(none listed)"}.${recentLine ? ` My experience: ${recentLine}` : ""}`,
    why_company: `About ${job.company}: ${(digest.about || []).join(" ") || "(nothing in the posting)"} My skills that match: ${skills.join(", ") || "(none listed)"}.`,
    experience: bodies.length ? `My experience: ${bodies.join(" ")}` : "",
  };
  answers.forEach((a, i) => {
    if (!facts[a.key].trim()) return;
    tasks.push({
      path: `answers.${i}.text`, kind: "answer", status: `Drafting answer ${i + 1} of ${answers.length}…`, maxTokens: 130,
      context: `${known} ${facts[a.key]} ${(digest.duties || []).join(" ")} ${evidence.map(roleOf).join(" ")}`,
      messages: [{ role: "system", content: ANSWER_SYSTEM }, ...ANSWER_EXAMPLES,
        { role: "user", content: `Question: ${a.question}\n${target}\nFacts: ${facts[a.key]}\nAnswer:` }],
    });
  });
  return { letter, tasks };
}

// ---------------------------------------------------------------- run the model over the plan
/**
 * Draft the letter and answers. ui.onRender(letter) repaints everything, ui.onBlock(path) one line,
 * ui.onStatus(text), ui.onTokens(stats). Returns { letter, stats }.
 * analysis.missing + approved decide the keywords the model may not use (missing ones you did not approve).
 */
export async function draftLetter({ llm, resume, job, analysis, approved = [], digest = {}, signal, ui }) {
  const { letter, tasks } = planLetter({ resume, job, analysis, approved, digest });
  const aliases = analysis.aliases || {};
  const approvedSet = new Set(approved);
  const forbidden = (analysis.missing || []).filter((m) => !approvedSet.has(m.keyword))
    .map((m) => ({ keyword: m.keyword, aliases: aliases[m.keyword] || [m.keyword] }));
  const stats = { rewritten: 0, flagged: 0, held: 0, tokens: 0, started: performance.now(), reasons: [] };
  ui.onRender(letter);

  for (const t of tasks) {
    if (signal?.aborted) break;
    ui.onStatus(t.status);
    const original = getAt(letter, t.path);
    letter.changes[t.path] = { orig: original, alt: null, state: "editing" };
    setAt(letter, t.path, "");
    ui.onBlock(t.path);
    let raw = "";
    try {
      for await (const delta of llm.stream(t.messages, { temperature: 0.3, max_tokens: t.maxTokens })) {
        raw += delta;
        stats.tokens++;
        setAt(letter, t.path, cleanParagraph(raw));
        ui.onBlock(t.path);
        ui.onTokens?.(stats);
        if (signal?.aborted || paragraphDone(raw)) llm.interrupt(); // one paragraph only
        if (signal?.aborted) break;
      }
    } catch (e) {
      console.warn(e);
    }
    const out = cleanParagraph(raw);
    const { hard, soft } = signal?.aborted ? { hard: "stopped", soft: [] }
      : validate(original, out, { forbidden, kind: t.kind, allowed: approved, extraContext: t.context, examples: EXAMPLE_TEXT });
    if (signal?.aborted || !out || out === original || /^(empty|too short|garbled)/.test(hard || "")) {
      setAt(letter, t.path, original);
      delete letter.changes[t.path];
      if (hard && !signal?.aborted) stats.reasons.push(hard);
    } else if (hard) {
      setAt(letter, t.path, original);
      letter.changes[t.path] = { orig: original, alt: out, state: "orig", held: hard };
      stats.held++;
      stats.reasons.push(hard);
    } else {
      setAt(letter, t.path, out);
      letter.changes[t.path] = { orig: original, alt: out, state: "alt", ...(soft.length ? { warn: soft.join("; ") } : {}) };
      stats.rewritten++;
      if (soft.length) stats.flagged++;
    }
    ui.onBlock(t.path);
  }
  stats.seconds = (performance.now() - stats.started) / 1000;
  return { letter, stats };
}

/** The letter as plain text (for copying). */
export function letterText(letter) {
  return [letter.greeting, ...letter.paragraphs, `${letter.signoff}\n${letter.name}`].filter((x) => x && x.trim()).join("\n\n");
}
