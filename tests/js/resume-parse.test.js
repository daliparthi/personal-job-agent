// Tests for building master_resume.yaml from an upload (static/js/resume-parse.js): which parts the model writes as
// loose YAML, what happens when the server turns a part down, and the live view.
// Run: npm test   (or: node --test "tests/js/**/*.test.js")
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseTasks, refineWithModel, yamlLines } from "../../static/js/resume-parse.js";

const job = (n, bullets = 2) => ({
  title: "", company: "", location: "", start: "", end: "", description: "",
  bullets: Array.from({ length: bullets }, (_, i) => `Built pipeline number ${n}-${i} that loads sales data into the warehouse every night.`),
  _src: [`Data Engineer ${n}, Contoso | 2018 - 2021`], _rest: `Data Engineer ${n}, Contoso`,
});
const lines = (entries) => entries.flatMap((e) => [...e._src, ...e.bullets.map((b) => `• ${b}`)]);

function draft(entries) {
  return {
    name: "", _header_rest: "",
    sections: [
      { title: "SKILLS", kind: "skills", groups: [], lines: ["Python, SQL"], _lines: ["Python, SQL"] },
      { title: "EXPERIENCE", kind: "experience", entries, _lines: lines(entries) },
    ],
  };
}

test("short sections are read whole; a long job section is read job by job", () => {
  const short = parseTasks(draft([job(1)]));
  assert.deepEqual(short.map((t) => [t.type, t.scope, t.kind]), [["loose", "section", "skills"], ["loose", "section", "experience"]]);
  const long = parseTasks(draft(Array.from({ length: 8 }, (_, i) => job(i, 3))));
  assert.equal(long.length, 1 + 8);
  assert.ok(long.slice(1).every((t) => t.scope === "entry" && t.source[0].startsWith("Data Engineer")));
  assert.match(long[1].label, /one job \(1 of 8\) in “EXPERIENCE”/);
});

test("the bundled model reads two or more jobs one at a time; an external one reads a short section whole", () => {
  const d = draft([job(1), job(2)]);
  assert.deepEqual(parseTasks(d).slice(1).map((t) => t.scope), ["entry", "entry"]);
  assert.deepEqual(parseTasks(d, { remote: true }).slice(1).map((t) => t.scope), ["section"]);
});

function fakeLlm(reply) {
  const asked = [];
  return {
    asked, backend: "onnx", interrupt() {},
    async *stream(messages, opts) {
      asked.push({ messages, opts });
      yield reply(messages.at(-1).content);
    },
  };
}
const ui = { onStatus() {}, onTokens() {}, onUpdate() {} };

test("a part the server accepts replaces the draft's; one it turns down keeps it and splits the headings", async () => {
  const d = draft([job(1)]);
  const llm = fakeLlm((prompt) => (prompt.startsWith("Heading:") ? "Title: Data Engineer 1\nCompany: Contoso\nLocation: none"
    : prompt.includes("skills section") ? "items: Python, SQL" : "entries:\n  - title: made up"));
  const checked = [];
  const check = async (body) => {
    checked.push(body);
    return body.kind === "skills" ? { ok: true, part: { title: "SKILLS", kind: "skills", groups: [{ name: "", items: ["Python", "SQL"] }], lines: [] } }
      : { ok: false, reason: "adds text that is not in your resume" };
  };
  const r = await refineWithModel({ llm, draft: d, signal: { aborted: false }, ui, check });
  assert.equal(r.parts, 1);
  assert.equal(r.kept.length, 1);
  assert.match(r.kept[0], /“EXPERIENCE”: adds text/);
  assert.deepEqual(d.sections[0].groups, [{ name: "", items: ["Python", "SQL"] }]);
  assert.deepEqual(d.sections[0]._lines, ["Python, SQL"]); // hints stay for the server, which drops them
  assert.equal(d.sections[1].entries[0].title, "Data Engineer 1"); // the heading split still ran
  assert.equal(d.sections[1].entries[0].company, "Contoso");
  assert.equal(checked[1].scope, "section");
  assert.deepEqual(checked[1].source, d.sections[1]._lines);
  assert.ok(llm.asked[0].opts.max_tokens > 60 && llm.asked[0].opts.max_tokens <= 1100);
});

test("the live view shows the YAML being written in place of its part", () => {
  const d = draft([job(1)]);
  const out = yamlLines(d, d.sections[0], "groups:\n  - name: Languages");
  const hot = out.filter((l) => l.hot).map((l) => l.text);
  assert.deepEqual(hot, ["  - title: SKILLS", "    kind: skills", "    groups:", "      - name: Languages"]);
  assert.ok(!out.some((l) => l.text.includes("_lines")));
});
