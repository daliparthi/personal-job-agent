// Tests for the cover letter and short answers (static/js/coverletter.js): the plain versions built from the resume,
// and the checks that hold back AI paragraphs that add facts.
// Run: npm test   (or: node --test "tests/js/**/*.test.js")
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  bodyTemplate, cleanParagraph, closing, draftLetter, joinList, letterText, opening, pickEvidence, planLetter, selfIntro,
} from "../../static/js/coverletter.js";

const RESUME = {
  name: "Jordan Avery",
  contact: { email: "jordan.avery@example.com" },
  sections: [
    { title: "Summary", kind: "summary", text: "Data engineer with 8 years of experience building data platforms on AWS. Enjoys mentoring." },
    { title: "Skills", kind: "skills", groups: [{ name: "Languages", items: ["Python", "SQL"] }], lines: [] },
    { title: "Experience", kind: "experience", entries: [
      { title: "Senior Data Engineer", company: "Northwind Analytics", start: "2021", end: "Present", bullets: [
        "Designed Spark and Airflow pipelines that process 3 TB of data per day.",
        "Migrated a legacy warehouse to Snowflake, cutting cost by 35%.",
        "Mentored four junior engineers.",
      ] },
      { title: "Data Analyst", company: "Fabrikam", start: "2016", end: "2021", bullets: [
        "Built SQL reports for the finance team.",
        "Responsible for the weekly KPI dashboard in Tableau.",
      ] },
    ] },
  ],
};
const JOB = { title: "Data Engineer", company: "Contoso" };
const ANALYSIS = {
  matched: ["Python", "SQL", "Airflow", "Snowflake"],
  missing: [{ keyword: "Kafka" }, { keyword: "dbt" }],
  aliases: { Python: ["Python"], SQL: ["SQL"], Airflow: ["Airflow"], Snowflake: ["Snowflake"], Kafka: ["Kafka"], dbt: ["dbt"] },
};
const DIGEST = { about: ["Contoso builds analytics software for hospitals."], duties: ["Build batch pipelines", "Own data quality"] };

/** A stand-in for LocalLLM that streams prepared replies, one per request. */
function fakeLLM(replies) {
  let i = 0;
  return {
    requests: [],
    interrupted: 0,
    async *stream(messages) {
      this.requests.push(messages);
      const reply = replies[i++] ?? "";
      for (const piece of reply.match(/\S+\s*|\s+/g) || []) yield piece;
    },
    interrupt() { this.interrupted++; },
  };
}
const ui = () => ({ renders: 0, blocks: [], statuses: [], onRender() { this.renders++; }, onBlock(p) { this.blocks.push(p); }, onStatus(s) { this.statuses.push(s); }, onTokens() {} });

// ---------------------------------------------------------------- plain versions
test("the opening introduces you from your summary", () => {
  assert.equal(selfIntro("Data engineer with 8 years of experience building data platforms on AWS. Enjoys mentoring."),
    "I am a data engineer with 8 years of experience building data platforms on AWS.");
  assert.equal(selfIntro("AWS-certified engineer with a focus on cost"), "I am an AWS-certified engineer with a focus on cost.");
  assert.equal(selfIntro("I build data platforms."), "I build data platforms.");
  assert.equal(selfIntro("Built three data platforms from scratch."), "");  // not a noun phrase: left out
  assert.equal(opening(JOB, RESUME),
    "I am writing to apply for the Data Engineer position at Contoso. I am a data engineer with 8 years of experience building data platforms on AWS.");
  assert.equal(opening(JOB, { sections: [] }), "I am writing to apply for the Data Engineer position at Contoso.");
});

test("the closing names only skills your resume and the posting share", () => {
  assert.equal(closing(JOB, ["Python", "SQL", "Airflow", "Snowflake"]),
    "I would welcome the chance to discuss how my experience with Python, SQL and Airflow could help Contoso. Thank you for your time and consideration.");
  assert.match(closing(JOB, []), /^I would welcome the chance to discuss how I could help Contoso\./);
  assert.equal(joinList(["a"]), "a");
  assert.equal(joinList(["a", "b"]), "a and b");
});

test("evidence: the entries and bullets that show what the posting asks for", () => {
  const ev = pickEvidence(RESUME, ANALYSIS);
  assert.equal(ev.length, 2);
  assert.equal(ev[0].entry.company, "Northwind Analytics");
  assert.deepEqual(ev[0].top.map((b) => b.k), [0, 1]);  // Airflow and Snowflake bullets, in resume order
  assert.equal(ev[1].entry.company, "Fabrikam");
  assert.deepEqual(pickEvidence(RESUME, { matched: ["Python"], aliases: {} }).length, 1);  // no match elsewhere
});

test("a body paragraph is made of your own bullets", () => {
  const [ev1, ev2] = pickEvidence(RESUME, ANALYSIS);
  assert.equal(bodyTemplate(ev1),
    "As Senior Data Engineer at Northwind Analytics, I designed Spark and Airflow pipelines that process 3 TB of data per day. I also migrated a legacy warehouse to Snowflake, cutting cost by 35%.");
  assert.equal(bodyTemplate(ev2),
    "As Data Analyst at Fabrikam, I built SQL reports for the finance team. Responsible for the weekly KPI dashboard in Tableau.");
});

test("the plan: letter, answers and the rewrites to ask for", () => {
  const { letter, tasks } = planLetter({ resume: RESUME, job: JOB, analysis: ANALYSIS, digest: DIGEST });
  assert.equal(letter.greeting, "Dear Contoso hiring team,");
  assert.equal(letter.paragraphs.length, 4);  // opening, two bodies, closing
  assert.equal(letter.name, "Jordan Avery");
  assert.match(letter.date, /^\d{4}-\d{2}-\d{2}$/);
  assert.deepEqual(letter.answers.map((a) => a.key), ["why_role", "why_company", "experience"]);
  assert.match(letter.answers[1].text, /how it describes its work: “Contoso builds analytics software for hospitals\.”/);
  assert.ok(letter.answers[1].hint);
  assert.deepEqual(tasks.map((t) => t.path), ["paragraphs.1", "paragraphs.2", "answers.0.text", "answers.1.text", "answers.2.text"]);
  assert.match(tasks[0].messages.at(-1).content, /^Target job: Data Engineer at Contoso\nRole: Senior Data Engineer at Northwind Analytics\nFacts: As Senior/);
  assert.match(letterText(letter), /^Dear Contoso hiring team,\n\nI am writing to apply/);
});

test("cleanParagraph drops labels, greetings and sign-offs", () => {
  assert.equal(cleanParagraph("Paragraph: As a data engineer, I built things."), "As a data engineer, I built things.");
  assert.equal(cleanParagraph("Dear Hiring Manager,\n\nAs a data engineer, I built things.\n\nSincerely,\nJordan"), "As a data engineer, I built things.");
  assert.equal(cleanParagraph("\"I built things.\""), "I built things.");
  assert.equal(cleanParagraph("Thank you,"), "");
});

// ---------------------------------------------------------------- the model's versions and the checks
const GOOD_P1 = "As a Senior Data Engineer at Northwind Analytics, I designed Spark and Airflow pipelines that process 3 TB of data per day and migrated a legacy warehouse to Snowflake, cutting cost by 35%.";
const INVENTED = "At Northwind Analytics I designed Spark pipelines for 40 teams and migrated a legacy warehouse to Snowflake, cutting cost by 35%.";
const USES_KAFKA = "At Fabrikam, I built SQL reports for the finance team and streamed them through Kafka every day.";

test("a faithful AI paragraph is used; one that invents a number or an unapproved keyword is held back", async () => {
  const llm = fakeLLM([GOOD_P1, USES_KAFKA, "", INVENTED, ""]);
  const u = ui();
  const { letter, stats } = await draftLetter({ llm, resume: RESUME, job: JOB, analysis: ANALYSIS, digest: DIGEST, signal: new AbortController().signal, ui: u });
  assert.equal(letter.paragraphs[1], GOOD_P1);
  assert.equal(letter.changes["paragraphs.1"].state, "alt");
  // Kafka is missing from the resume and not approved: the plain paragraph stays, the AI one waits behind a button
  assert.match(letter.paragraphs[2], /^As Data Analyst at Fabrikam, I built SQL reports/);
  assert.equal(letter.changes["paragraphs.2"].state, "orig");
  assert.match(letter.changes["paragraphs.2"].held, /uses “Kafka”, which you did not approve/);
  // an empty reply leaves the plain answer with no change record
  assert.equal(letter.changes["answers.0.text"], undefined);
  assert.match(letter.answers[0].text, /^The Data Engineer role lines up with the work I have done with Python, SQL and Airflow\./);
  // "40" is in none of your resume lines
  assert.match(letter.changes["answers.1.text"].held, /adds the number 40/);
  assert.equal(stats.rewritten, 1);
  assert.equal(stats.held, 2);
  assert.equal(llm.requests.length, 5);
  assert.ok(u.renders >= 1 && u.blocks.includes("paragraphs.1"));
});

test("an approved keyword may be used", async () => {
  const llm = fakeLLM(["", USES_KAFKA]);
  const { letter } = await draftLetter({ llm, resume: RESUME, job: JOB, analysis: ANALYSIS, approved: ["Kafka"], digest: DIGEST, signal: new AbortController().signal, ui: ui() });
  assert.equal(letter.paragraphs[2], USES_KAFKA);
  assert.equal(letter.changes["paragraphs.2"].state, "alt");
});

test("only one paragraph is taken from a reply", async () => {
  const llm = fakeLLM([`${GOOD_P1}\n\nSincerely,\nJordan Avery\n\nP.S. Another paragraph.`]);
  const { letter } = await draftLetter({ llm, resume: RESUME, job: JOB, analysis: ANALYSIS, digest: DIGEST, signal: new AbortController().signal, ui: ui() });
  assert.equal(letter.paragraphs[1], GOOD_P1);
  assert.ok(llm.interrupted > 0);
});

test("stopping keeps the plain versions", async () => {
  const ctl = new AbortController();
  const llm = fakeLLM([GOOD_P1, GOOD_P1]);
  const u = ui();
  u.onBlock = (p) => { if (p === "paragraphs.1") ctl.abort(); };
  const { letter } = await draftLetter({ llm, resume: RESUME, job: JOB, analysis: ANALYSIS, digest: DIGEST, signal: ctl.signal, ui: u });
  assert.match(letter.paragraphs[1], /^As Senior Data Engineer at Northwind Analytics, I designed/);
  assert.deepEqual(letter.changes, {});
  assert.equal(llm.requests.length, 1);
});

test("a list reply becomes one sentence; an unfinished one is dropped", () => {
  assert.equal(cleanParagraph("My relevant experience includes:\n- Spark pipelines\n- Snowflake migration\n- dbt tests"),
    "My relevant experience includes Spark pipelines, Snowflake migration and dbt tests.");
  assert.equal(cleanParagraph("Relevant experience for the Data Engineer role includes:"), "");
  assert.equal(cleanParagraph("I designed Spark pipelines. I also migrated a warehouse to Snow"), "I designed Spark pipelines.");
  assert.equal(cleanParagraph("I designed Spark pipelines"), "");
});

test("an answer cut short at a colon keeps the plain version", async () => {
  const llm = fakeLLM(["", "", "", "", "Relevant experience for the Data Engineer role at Contoso includes:"]);
  const { letter } = await draftLetter({ llm, resume: RESUME, job: JOB, analysis: ANALYSIS, digest: DIGEST, signal: new AbortController().signal, ui: ui() });
  assert.equal(letter.changes["answers.2.text"], undefined);
  assert.match(letter.answers[2].text, /^As Senior Data Engineer at Northwind Analytics, I designed/);
});

test("a body paragraph that retells a fact in new words is applied but marked to check", async () => {
  const garbled = "As Senior Data Engineer at Northwind Analytics, I designed Spark and Airflow pipelines that process 3 TB of data per day. Additionally, I migrated a legacy warehouse to Snowflake to reduce the likelihood of 35% of costs being wasted due to legacy issues.";
  const llm = fakeLLM([garbled]);
  const { letter } = await draftLetter({ llm, resume: RESUME, job: JOB, analysis: ANALYSIS, digest: DIGEST, signal: new AbortController().signal, ui: ui() });
  assert.equal(letter.changes["paragraphs.1"].state, "alt");
  assert.match(letter.changes["paragraphs.1"].warn, /adds words not in your resume/);
});
