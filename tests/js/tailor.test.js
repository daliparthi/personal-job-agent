// Tests for the checks that keep the local model from adding facts to a resume (static/js/tailor.js).
// Run: npm test   (or: node --test "tests/js/**/*.test.js")
import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanOutput, tailorResume, validate } from "../../static/js/tailor.js";

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

// ---- an external model gets the posting's own sentence for each approved keyword
const master = () => ({
  sections: [
    { kind: "summary", text: "Software engineer with 5 years of experience building web services and data pipelines." },
    { kind: "projects", entries: [{ name: "Billing", bullets: ["Built web services for the billing team that handle invoices every night."] }] },
  ],
});
const analysis = {
  matched: [], aliases: {},
  missing: [{ keyword: "Kubernetes", weight: 3, count: 1, context: 'deploy and scale services on "Kubernetes" clusters' }],
};

async function tailorWith(backend) {
  const prompts = [];
  const llm = {
    backend, interrupt() {},
    async *stream(messages, opts) {
      prompts.push({ messages, opts });
      yield "Software engineer with 5 years of experience building web services, deployed on Kubernetes clusters.";
    },
  };
  const ui = { onRender() {}, onBlock() {}, onStatus() {}, onTokens() {} };
  const out = await tailorResume({ llm, master: master(), job: { title: "Backend Engineer", company: "Initech" },
    analysis, approved: ["Kubernetes"], signal: { aborted: false }, ui });
  return { prompts, out };
}

test("an external model is shown where the posting uses each keyword, and may write more", async () => {
  const { prompts, out } = await tailorWith("remote");
  const summary = prompts[0];
  assert.match(summary.messages.at(-1).content, /Must include: Kubernetes \(the posting says: "deploy and scale services on Kubernetes clusters"\)/);
  assert.equal(summary.messages.length, 2); // system + user: no few-shot examples
  assert.equal(summary.opts.max_tokens, 280);
  assert.match(out.res.sections[0].text, /Kubernetes clusters/);
});

test("the bundled model keeps the short prompt and its examples", async () => {
  const { prompts } = await tailorWith("onnx");
  assert.equal(prompts[0].messages.at(-1).content.includes("the posting says"), false);
  assert.ok(prompts[0].messages.length > 2);
  assert.equal(prompts[0].opts.max_tokens, 170);
});
