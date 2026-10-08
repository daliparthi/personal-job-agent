// Tests for the checks that keep the local model from adding facts to a resume (static/js/tailor.js).
// Run: npm test   (or: node --test "tests/js/**/*.test.js")
import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanOutput, tailorResume, validate, validateNew } from "../../static/js/tailor.js";

const opts = (over = {}) => ({ forbidden: [], kind: "bullet", allowed: [], extraContext: "", required: [], ...over });

test("a faithful rewrite passes with no warnings", () => {
  const r = validate("set up nightly backups for the payments database, restore time down 40%",
    "Automated nightly backups for the payments database, cutting restore time by 40%.", opts());
  assert.equal(r.hard, null);
  assert.deepEqual(r.soft, []);
});

test("a new number is held back", () => {
  const r = validate("Built pipelines for 3 teams.", "Built pipelines for 5 teams across 2 regions.", opts());
  assert.match(r.hard, /adds the number 5/);
});

test("a keyword you rejected is held back", () => {
  const forbidden = [{ keyword: "Kubernetes", aliases: ["Kubernetes", "K8s"] }];
  const r = validate("Deployed billing services on Docker hosts.", "Deployed billing services on Docker hosts and K8s.",
    opts({ forbidden }));
  assert.match(r.hard, /uses “Kubernetes”, which you did not approve/);
});

test("an approved keyword is allowed", () => {
  const r = validate("Loaded clickstream events into the reporting store every hour.",
    "Built ETL jobs that loaded clickstream events into the reporting store every hour.", opts({ allowed: ["ETL"] }));
  assert.equal(r.hard, null);
});

test("digits inside a name are not new numbers", () => {
  const r = validate("Deployed billing services on Docker hosts.", "Deployed billing services on Docker hosts and K8s.",
    opts({ allowed: ["Kubernetes", "K8s"] }));
  assert.equal(r.hard, null);
  assert.match(validate("Stored files in S3.", "Stored 40 TB of files in S3.", opts()).hard, /adds the number 40/);
});

test("a new tool or product name is held back", () => {
  const r = validate("Built reporting pipelines in Python.", "Built reporting pipelines in Python and Snowflake.", opts());
  assert.match(r.hard, /adds “Snowflake”/);
});

test("echoing the prompt is dropped as garbled", () => {
  const r = validate("Built pipelines.", "Original bullet: Built pipelines. Rewritten bullet: Built pipelines.", opts());
  assert.match(r.hard, /garbled \(repeated the instructions\)/);
});

test("copying the few-shot example is dropped as garbled", () => {
  const r = validate("Wrote nightly reports for finance managers.",
    "Built ETL pipelines that load sales data every night for finance managers.", opts());
  assert.match(r.hard, /garbled/);
});

test("repetition, empty and too-short outputs are dropped", () => {
  const original = "Designed data quality checks with alerting for analytics dashboards.";
  assert.match(validate(original, "the data quality checks the data quality checks the data quality checks", opts()).hard,
    /repeats itself|too short|adds/);
  assert.equal(validate(original, "", opts()).hard, "empty output");
  assert.equal(validate(original, "Designed.", opts()).hard, "too short");
});

test("dropping a skill the job wants is flagged for review", () => {
  const required = [{ keyword: "Airflow", aliases: ["Airflow", "Apache Airflow"] }];
  const r = validate("Scheduled pipelines with Airflow every hour.", "Scheduled data pipelines every hour.", opts({ required }));
  assert.equal(r.hard, null);
  assert.ok(r.soft.some((s) => s.includes("dropped “Airflow”")));
});

test("moving a tool to a different role in the sentence is flagged", () => {
  const r = validate("Migrated a legacy warehouse to Snowflake.", "Migrated Snowflake from a legacy warehouse.", opts());
  assert.ok(r.soft.some((s) => s.startsWith("moved “Snowflake”")), r.soft.join("; "));
});

test("cleanOutput strips chatter, labels, list markers and quotes", () => {
  assert.equal(cleanOutput("Here's the rewritten bullet: Built X."), "Built X.");
  assert.equal(cleanOutput("Rewritten bullet: Built X."), "Built X.");
  assert.equal(cleanOutput("- Built X\nSecond line"), "Built X");
  assert.equal(cleanOutput("“Built X”"), "Built X");
  assert.equal(cleanOutput("1) Built X"), "Built X");
  assert.equal(cleanOutput(null), "");
});

// ---- new sentences for approved keywords
const kw = (keyword, aliases = [keyword]) => ({ keyword, aliases });

test("a new sentence that shows its keywords from your own work passes", () => {
  const r = validateNew("Packaged the billing web services with Kubernetes so invoices are handled every night.", {
    keywords: [kw("Kubernetes", ["Kubernetes", "K8s"])], context: "Built web services for the billing team that handle invoices every night.",
  });
  assert.equal(r.hard, null);
  assert.deepEqual(r.soft, []);
});

test("a new sentence may not add numbers, names, rejected keywords or leave out its keywords", () => {
  const context = "Built web services for the billing team.";
  const keywords = [kw("Kubernetes")];
  assert.match(validateNew("Deployed 40 billing web services on Kubernetes clusters.", { keywords, context }).hard, /adds the number 40/);
  assert.match(validateNew("Deployed the billing web services on Kubernetes and Terraform.", { keywords, context }).hard, /adds “Terraform”/);
  assert.match(validateNew("Deployed the billing web services on Kubernetes with Helm charts.",
    { keywords, context, forbidden: [kw("Helm")] }).hard, /uses “Helm”, which you did not approve/);
  assert.match(validateNew("Deployed the billing web services for the whole team.", { keywords, context }).hard, /left out the keywords/);
});

test("a soft skill never shares a sentence with a tool", () => {
  const context = "Data engineer building pipelines.";
  const mixedTech = validateNew("Hands on experience with dbt, critical thinking and data pipelines.",
    { keywords: [kw("dbt")], mixed: [kw("Critical thinking")], context, kind: "summary" });
  assert.match(mixedTech.hard, /mixes the soft skill “Critical thinking” into a technical sentence/);
  const mixedSoft = validateNew("Known for critical thinking when building dbt pipelines with partners.",
    { keywords: [kw("Critical thinking")], mixed: [kw("dbt")], context, kind: "soft" });
  assert.match(mixedSoft.hard, /names “dbt” in a soft-skills sentence/);
});

const master = () => ({
  sections: [
    { kind: "summary", text: "Software engineer with 5 years of experience building web services and data pipelines." },
    { kind: "projects", entries: [{ name: "Billing", bullets: ["Built web services for the billing team that handle invoices every night."] }] },
  ],
});
const analysis = {
  matched: [], aliases: { "Critical thinking": ["Critical thinking"] }, soft: ["Critical thinking"],
  missing: [
    { keyword: "Kubernetes", weight: 3, count: 1, context: 'deploy and scale services on "Kubernetes" clusters' },
    { keyword: "Critical thinking", weight: 1, count: 1, soft: true, context: "strong critical thinking" },
  ],
};

/** Answers each kind of prompt with `replies[kind]` (summary, tech, soft, bullet, rewrite). */
async function tailorWith(backend, replies = {}, { approved = ["Kubernetes", "Critical thinking"], data = master(), jd = analysis } = {}) {
  const prompts = [];
  const kindOf = (m) => (m.includes("Tailored summary:") ? "summary" : m.includes("Soft skills to show:") ? "soft"
    : m.includes("New sentence:") ? "tech" : m.includes("New bullet:") ? "bullet" : "rewrite");
  const llm = {
    backend, interrupt() {},
    async *stream(messages, opts) {
      const kind = kindOf(messages.at(-1).content);
      prompts.push({ kind, messages, opts });
      const text = replies[kind] ?? "";
      if (text) yield text;
    },
  };
  const ui = { onRender() {}, onBlock() {}, onStatus() {}, onTokens() {} };
  const out = await tailorResume({ llm, master: data, job: { title: "Backend Engineer", company: "Initech" },
    analysis: jd, approved, signal: { aborted: false }, ui });
  return { prompts, out };
}

const GOOD = {
  tech: "Runs the web services and data pipelines on Kubernetes.",
  soft: "Known for critical thinking when building web services with product teams.",
};

test("hard and soft keywords get their own new sentences at the end of the summary", async () => {
  const { out } = await tailorWith("onnx", GOOD);
  const text = out.res.sections[0].text;
  assert.ok(text.startsWith("Software engineer with 5 years of experience"));
  assert.ok(text.includes(GOOD.tech) && text.includes(GOOD.soft), text);
  for (const sentence of text.split(/(?<=\.)\s+/)) {
    assert.ok(!(/Kubernetes/.test(sentence) && /critical thinking/i.test(sentence)), sentence);
  }
  assert.equal(out.changes["sections.0.text"].orig, master().sections[0].text);
});

test("a technical sentence that mixes in a soft skill is replaced by plain separate sentences", async () => {
  const { out } = await tailorWith("onnx", { tech: "Hands on experience with Kubernetes, critical thinking and web services." });
  const text = out.res.sections[0].text;
  assert.ok(text.includes("Hands-on experience with Kubernetes."), text);
  assert.ok(text.includes("Known for critical thinking."), text);
  assert.match(out.changes["sections.0.text"].warn, /written from a template \(the AI version mixed the soft skill/);
});

test("keywords beyond the summary's get new project bullets; existing bullets get none", async () => {
  const jd = {
    matched: [], aliases: {}, soft: [],
    missing: ["Kubernetes", "Terraform", "Helm", "Kafka"].map((keyword, i) => ({ keyword, weight: 4 - i, count: 1, context: "" })),
  };
  const replies = { tech: "Runs the web services on Kubernetes, Terraform and Helm.",
    bullet: "Streamed the nightly billing invoices through Kafka topics for the web services." };
  const { out } = await tailorWith("onnx", replies, { approved: ["Kubernetes", "Terraform", "Helm", "Kafka"], jd });
  const bullets = out.res.sections[1].entries[0].bullets;
  assert.equal(bullets[0], master().sections[1].entries[0].bullets[0]);
  assert.equal(bullets.length, 2);
  assert.equal(bullets[1], replies.bullet);
  assert.equal(out.changes["sections.1.entries.0.bullets.1"].orig, null);
  assert.ok(!out.res.sections[0].text.includes("Kafka"));
});

test("a model that writes nothing still gives every keyword a sentence of its own", async () => {
  const jd = { ...analysis, missing: [...analysis.missing, { keyword: "Kafka", weight: 0.5, count: 1, context: "" }] };
  const data = master();
  const { out } = await tailorWith("onnx", {}, { approved: ["Kubernetes", "Critical thinking", "Kafka"], jd, data });
  const text = out.res.sections[0].text;
  assert.match(text, /Hands-on experience with Kubernetes and Kafka\./);
  assert.match(text, /Known for critical thinking\./);
  assert.equal(out.res.sections[1].entries[0].bullets.length, 1); // nothing pushed into, or added to, the project
});

test("an external model is shown where the posting uses each keyword, without examples", async () => {
  const { prompts } = await tailorWith("remote", GOOD);
  const tech = prompts.find((p) => p.kind === "tech");
  assert.match(tech.messages.at(-1).content, /Skills to show: Kubernetes \(the posting says: "deploy and scale services on Kubernetes clusters"\)/);
  assert.equal(tech.messages.length, 2); // system + user: no few-shot examples
  const summary = prompts.find((p) => p.kind === "summary");
  assert.match(summary.messages.at(-1).content, /Must include: \(none\)/);
});

test("the bundled model keeps the short prompts and their examples", async () => {
  const { prompts } = await tailorWith("onnx", GOOD);
  const tech = prompts.find((p) => p.kind === "tech");
  assert.equal(tech.messages.at(-1).content.includes("the posting says"), false);
  assert.ok(tech.messages.length > 2);
  assert.equal(prompts.find((p) => p.kind === "summary").opts.max_tokens, 170);
});
