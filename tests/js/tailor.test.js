// Tests for the checks that keep the local model from adding facts to a resume (static/js/tailor.js).
// Run: npm test   (or: node --test "tests/js/**/*.test.js")
import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanOutput, validate } from "../../static/js/tailor.js";

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
