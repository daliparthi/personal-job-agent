// Injected into Workday application pages opened by Job Agent.
// Fills standard fields from your profile, your work history and education from master_resume.yaml, voluntary
// disclosures ("decline to answer" unless you changed it in Settings) and custom questions you answered before (from
// the answer bank, outlined amber for you to check). It never overwrites something you typed, never refills a field
// you have touched (so you can clear or retype what it filled), and it never clicks
// Create Account, Sign In, Next or Submit, or ticks a consent or signature box. The one button it may click is
// Workday's "Add" / "Add Another" in the Work Experience and Education sections, and only when Settings allows it.
// (The Create Account / Sign In email + password come from your .env file and are typed by the Python side,
//  so the password never passes through this script.)
// Field patterns live in app/autofill_rules.json.
(() => {
  if (window.__jobAgentLoaded) return;
  window.__jobAgentLoaded = true;
  if (!/myworkday(jobs|site)\.com$/i.test(location.hostname)) return;

  const STATE_NAMES = {AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",CO:"Colorado",CT:"Connecticut",DE:"Delaware",DC:"District of Columbia",FL:"Florida",GA:"Georgia",HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",KY:"Kentucky",LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",MT:"Montana",NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",NY:"New York",NC:"North Carolina",ND:"North Dakota",OH:"Ohio",OK:"Oklahoma",OR:"Oregon",PA:"Pennsylvania",PR:"Puerto Rico",RI:"Rhode Island",SC:"South Carolina",SD:"South Dakota",TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",VA:"Virginia",WA:"Washington",WV:"West Virginia",WI:"Wisconsin",WY:"Wyoming"};
  const MAX_DROPDOWNS_PER_TICK = 10;
  const AMBER = "#f59e0b";

  let setup = null;   // { profile, rules, history, options, disclosures, job } from Job Agent
  let C = null;       // compiled rules
  let profile = null;
  let filled = 0;
  let fromBank = 0;
  let busy = false;
  let reportedApplied = false;
  const gaveUp = new WeakSet();
  const touched = new WeakSet();     // fields you focused, typed in or clicked: left alone from then on, even if emptied
  let scripted = 0;                  // until this time, focus events come from this script (the banner), not from you
  const bankFilled = new WeakMap();  // element -> the value filled from the answer bank
  const asked = new Map();           // question -> saved answer (or null)
  const used = new Set();            // answer ids already reported as used
  const sentAnswers = new Map();     // question -> last answer captured
  const addClicks = { exp: 0, edu: 0 };
  let lastListbox = null;

  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
  const visible = (el) => !!(el && el.offsetParent !== null && el.getClientRects().length);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const event = (kind, payload) => (window.__jobAgentEvent ? window.__jobAgentEvent(kind, payload) : Promise.resolve(null));
  const rx = (p) => (p ? new RegExp(p, "i") : null);
  const placeholder = (s) => !s || /^select one|^select$|^choose|^none selected/.test(s);

  function compile(r) {
    const fields = (f) => Object.fromEntries(Object.entries(f || {}).map(([k, p]) => [k, rx(p)]));
    return {
      text: (r.text || []).map(([k, p, n]) => [k, rx(p), rx(n)]),
      choices: (r.choices || []).map(([k, p, n]) => [k, rx(p), rx(n)]),
      submitted: rx(r.submitted),
      exp: { section: rx(r.experience.section), entry: rx(r.experience.entry), f: fields(r.experience.fields) },
      edu: { section: rx(r.education.section), entry: rx(r.education.entry), f: fields(r.education.fields) },
      add: rx(r.add_button), never: rx(r.never_click), decline: rx(r.decline),
      skills: rx(r.skills_field || "\\bskills?\\b"), skillOption: r.skill_option || '[role="option"]',
      disclosures: Object.entries(r.disclosures || {}).map(([k, p]) => [k, rx(p)]),
      disclosureOptions: Object.fromEntries(Object.entries(r.disclosure_options || {})
        .map(([kind, opts]) => [kind, Object.fromEntries(Object.entries(opts).map(([k, p]) => [k, rx(p)]))])),
    };
  }

  // ---------------------------------------------------------------- what a field is
  function describe(el) {
    const parts = [el.id, el.name, el.getAttribute("data-automation-id"), el.getAttribute("aria-label"), el.placeholder];
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) parts.push(l.innerText);
    }
    (el.getAttribute("aria-labelledby") || "").split(/\s+/).forEach((id) => {
      const n = id && document.getElementById(id);
      if (n && n !== el) parts.push(n.innerText);
    });
    const wrap = el.closest('[data-automation-id^="formField"]');
    if (wrap) {
      parts.push(wrap.getAttribute("data-automation-id"));
      const l = wrap.querySelector("label, legend");
      if (l) parts.push(l.innerText);
    }
    const fs = el.closest("fieldset");
    if (fs) {
      const lg = fs.querySelector("legend");
      if (lg) parts.push(lg.innerText);
    }
    return norm(parts.filter(Boolean).join(" | "));
  }

  const cleanLabel = (s) => (s || "").replace(/\*/g, " ").replace(/\(?\brequired\b\)?/gi, " ").replace(/\s+/g, " ").trim();

  /** The question a field asks, as a person reads it (its label), or "". Raw keeps the "*" required marker. */
  function labelOf(el, raw = false) {
    const parts = [];
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) parts.push(l.innerText);
    }
    if (!parts.length) {
      (el.getAttribute("aria-labelledby") || "").split(/\s+/).forEach((id) => {
        const n = id && document.getElementById(id);
        if (n && n !== el && !el.contains(n)) parts.push(n.innerText);
      });
    }
    if (!parts.length) {
      const wrap = el.closest('[data-automation-id^="formField"], fieldset, [role="radiogroup"], [role="group"]');
      const l = wrap && wrap.querySelector("label, legend");
      if (l && !(l.htmlFor && l.htmlFor === el.id)) parts.push(l.innerText);
    }
    if (!parts.length && el.getAttribute("aria-label")) parts.push(el.getAttribute("aria-label"));
    const text = parts.join(" ").replace(/\s+/g, " ").trim();
    return raw ? text : cleanLabel(text);
  }

  /** A radio or checkbox group's question: its fieldset legend or the form field label around it. */
  function groupLabel(input, raw = false) {
    const box = input.closest('fieldset, [role="radiogroup"], [role="group"], [data-automation-id^="formField"]');
    if (box) {
      const ids = (box.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
      for (const id of ids) {
        const n = document.getElementById(id);
        if (n) return raw ? n.innerText.trim() : cleanLabel(n.innerText);
      }
      const l = box.querySelector("legend, label:not([for])") || box.querySelector("label");
      if (l && !(l.htmlFor && l.htmlFor === input.id)) return raw ? l.innerText.trim() : cleanLabel(l.innerText);
    }
    return "";
  }

  const optionLabel = (input) => {
    const l = input.id && document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
    return norm((l && l.innerText) || input.getAttribute("aria-label") || input.value);
  };

  /** The profile rule for a choice question: decided by the question's own label when that names a rule, so text
   *  around it (ids, a wrapper holding other questions) can't turn it into a different question. [re, value, not, key]. */
  function ruleFor(label, d) {
    const rules = choiceRules();
    const pick = (text) => text && rules.find(([re, v, not]) => v && re.test(text) && !(not && not.test(text)));
    return pick(norm(label)) || pick(d) || null;
  }

  function choiceRules() {
    const values = {
      how_heard: profile.how_heard, phone_type: profile.phone_type, authorized_us: profile.authorized_us,
      needs_sponsorship: profile.needs_sponsorship, previously_employed: profile.previously_employed,
      state: STATE_NAMES[(profile.state || "").toUpperCase()] || profile.state, country: profile.country,
    };
    return C.choices.map(([k, re, not]) => [re, values[k], not, k]);
  }

  const disclosureOf = (d) => (C.disclosures.find(([, re]) => re.test(d)) || [null])[0];
  /** The option to pick for a disclosure question (a RegExp), or null to leave it to you: Settings says "Leave for me",
   *  or the answer you chose (say "Male") is not one of the rules' options. Anything but a chosen answer declines. */
  function disclosureWanted(kind) {
    const choice = kind && (setup.disclosures || {})[kind];
    if (!kind || choice === "skip") return null;
    if (!choice || choice === "decline") return C.decline;
    return (C.disclosureOptions[kind] || {})[choice] || null;
  }

  /** Fields the rules fill from the profile, history or disclosure settings (everything else is a custom question). */
  function isStandard(el, d = describe(el)) {
    if (C.text.some(([, re, not]) => re.test(d) && !(not && not.test(d)))) return true;
    if (C.choices.some(([, re, not]) => re.test(d) && !(not && not.test(d)))) return true;
    if (el.tagName === "INPUT" && C.skills.test(d)) return true;
    return !!disclosureOf(d);
  }

  // ---------------------------------------------------------------- doing things to the page
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  /**
   * The only way this script clicks. kind: "listbox" (open a dropdown), "option" (pick from an open dropdown),
   * "radio", "checkbox", or "add" (Workday's Add / Add Another in Work Experience or Education). Submit buttons,
   * links, and anything labelled like Next, Submit, Sign In, Create Account, Agree or Consent are refused.
   */
  function safeClick(el, kind) {
    if (!el || el.type === "submit" || (el.tagName === "A" && el.getAttribute("href"))) return false;
    const text = norm(el.innerText || el.getAttribute("aria-label") || el.value || "");
    if (kind === "listbox" && el.getAttribute("aria-haspopup") !== "listbox") return false;
    if (kind === "add" && !(C.add.test(text) && el.tagName === "BUTTON")) return false;
    if ((kind === "radio" || kind === "checkbox") && C.never.test(optionLabel(el))) return false;
    if ((kind === "add" || kind === "radio" || kind === "checkbox") && C.never.test(text) && !C.add.test(text)) return false;
    el.click();
    return true;
  }

  /** Open a dropdown and pick the first option matching `wanted` (a string, a list of strings tried in order, or a RegExp). */
  async function pickListbox(button, wanted) {
    if (!safeClick(button, "listbox")) return false;
    for (let i = 0; i < 15; i++) {
      await sleep(120);
      const opts = [...document.querySelectorAll('[role="option"]')].filter(visible);
      if (!opts.length) continue;
      let hit = null;
      if (wanted instanceof RegExp) hit = opts.find((o) => wanted.test(o.innerText));
      else {
        for (const w of (Array.isArray(wanted) ? wanted : [wanted]).map(norm).filter(Boolean)) {
          hit = opts.find((o) => norm(o.innerText) === w) || opts.find((o) => norm(o.innerText).startsWith(w));
          if (hit) break;
        }
      }
      if (hit) {
        safeClick(hit, "option");
        return true;
      }
      break;
    }
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.body.click();
    return false;
  }

  function markFromBank(el, match, value) {
    el.style.outline = `2px solid ${AMBER}`;
    el.style.outlineOffset = "2px";
    el.title = `Job Agent filled this ${match.note}. Check it says what you mean.`;
    bankFilled.set(el, value);
    fromBank++;
    if (!used.has(match.id)) {
      used.add(match.id);
      event("used", [match.id]);
    }
  }

  // ---------------------------------------------------------------- sections: work experience and education
  function headingOf(g) {
    for (const id of (g.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)) {
      const n = document.getElementById(id);
      if (n) return norm(n.innerText);
    }
    const h = g.querySelector(":scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > legend, :scope > div > h3, :scope > div > h4");
    return h ? norm(h.innerText) : "";
  }
  const groupsMatching = (re) => [...document.querySelectorAll('[role="group"], fieldset, section')].filter((g) => re.test(headingOf(g)));

  const two = (n) => String(n).padStart(2, "0");

  function dateValue(el, d, end, yearsOnly) {
    if (end && d.current) return null;
    const year = end ? d.end_year : d.start_year;
    const month = yearsOnly ? null : end ? d.end_month : d.start_month;
    const what = norm(`${el.getAttribute("data-automation-id") || ""} ${el.getAttribute("aria-label") || ""} ${el.placeholder || ""}`);
    if (!year) return null;
    if (/month/.test(what)) return month ? two(month) : null;
    if (/year|yyyy/.test(what) && !/mm/.test(what)) return String(year);
    if (/mm/.test(what) || !yearsOnly) return month ? `${two(month)}/${year}` : null; // never invent a month
    return String(year);
  }

  /** "from" / "to" for a date input, from its form field's label (not the input's own Month/Year label). */
  function dateSide(el, F) {
    const wrap = el.closest('[data-automation-id^="formField"]');
    const label = norm(`${wrap ? wrap.getAttribute("data-automation-id") : ""} ${wrap ? (wrap.querySelector("label, legend")?.innerText || "") : labelOf(el)}`);
    if (F.to.test(label) && !F.from.test(label)) return "to";
    if (F.from.test(label)) return "from";
    return null;
  }

  async function fillEntry(g, d, kind) {
    const F = (kind === "exp" ? C.exp : C.edu).f;
    for (const el of g.querySelectorAll('input, textarea, button[aria-haspopup="listbox"]')) {
      if (el.disabled || el.readOnly || touched.has(el)) continue;
      const desc = describe(el);
      if (el.type === "checkbox") {
        if (kind === "exp" && F.current.test(desc) && d.current && !el.checked && safeClick(el, "checkbox")) filled++;
        continue;
      }
      if (!visible(el) || el.type === "radio" || el.type === "file" || el.type === "hidden") continue;
      if (el.tagName === "BUTTON") {
        if (kind === "edu" && F.degree.test(desc) && placeholder(norm(el.innerText)) && !gaveUp.has(el) && d.degree_options?.length) {
          if (await pickListbox(el, d.degree_options)) filled++; else gaveUp.add(el);
        }
        continue;
      }
      if (el.value) continue;
      const side = dateSide(el, F);
      let value = null;
      if (side) value = dateValue(el, d, side === "to", kind === "edu");
      else if (el.tagName === "TEXTAREA") value = kind === "exp" && F.description.test(desc) ? d.description : null;
      else if (kind === "exp") {
        if (F.title.test(desc)) value = d.title;
        else if (F.company.test(desc)) value = d.company;
        else if (F.location.test(desc)) value = d.location;
      } else if (F.school.test(desc)) value = d.school;
      else if (F.field.test(desc)) value = d.field;
      else if (F.gpa.test(desc)) value = d.gpa;
      if (value) {
        setNativeValue(el, value);
        filled++;
      }
    }
  }

  /** Fill each Work Experience / Education entry on the page with the matching resume entry (most recent first),
   *  and (when Settings allows) click the section's Add button until there are as many entries as on your resume. */
  async function fillHistory(kind) {
    const S = kind === "exp" ? C.exp : C.edu;
    const items = (setup.history || {})[kind === "exp" ? "experience" : "education"] || [];
    if (!items.length) return { wanted: 0, have: 0 };
    const entries = groupsMatching(S.entry);
    for (let i = 0; i < Math.min(entries.length, items.length); i++) await fillEntry(entries[i], items[i], kind);
    const sections = groupsMatching(S.section);
    if (sections.length && entries.length < items.length && setup.options?.add_entries && addClicks[kind] <= items.length) {
      const sec = sections[0];
      const btn = [...sec.querySelectorAll("button")].filter(visible)
        .find((b) => C.add.test(b.innerText || b.getAttribute("aria-label") || "") && !entries.some((e) => e.contains(b)));
      if (btn && safeClick(btn, "add")) addClicks[kind]++;
    }
    return { wanted: sections.length ? items.length : 0, have: entries.length };
  }

  /** Workday's "Type to Add Skills" box: type each skill of the tailored resume, press Enter and pick the suggestion
   *  that is that skill (never a different one). A few per pass; a skill Workday doesn't list is skipped.
   *  Workday keeps the last search's list on screen until the new one arrives, and redraws it as results come in, so
   *  a suggestion is picked only from this search's settled list, and the pick is checked (and made again on the
   *  redrawn list) until the skill shows as selected. */
  const skillsDone = new Set();   // skills added, already there, or not listed by Workday: not searched again
  const skillTries = new Map();   // skill -> searches that got no list at all (Workday was slow): tried again once
  const skillsMissing = [];       // skills Workday doesn't list, for the banner
  let skillsAdded = 0;
  const MAX_SKILLS = 60, SKILLS_PER_TICK = 8;
  // Compared without case and punctuation: "Fine Tuning" is Workday's "Fine-Tuning".
  const simple = (s) => norm(s).replace(/[^a-z0-9+#]+/g, " ").trim();
  /** The suggestion that is this skill: exact, else one that starts with it ("Python (Programming Language)"), else one
   *  that has it as a whole word ("Apache Spark"); the shortest of those. null when nothing names the skill. */
  function bestSkillOption(opts, skill) {
    const s = simple(skill);
    if (!s) return null;
    const whole = new RegExp(`(^| )${s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}( |$)`);
    const text = (o) => simple(o.innerText);
    const plain = (o) => simple(norm(o.innerText).replace(/\([^)]*\)/g, " "));
    const shortest = (list) => list.sort((a, b) => text(a).length - text(b).length)[0] || null;
    return opts.find((o) => text(o) === s) || opts.find((o) => plain(o) === s)
      || shortest(opts.filter((o) => text(o).startsWith(s) && whole.test(text(o))))
      || shortest(opts.filter((o) => whole.test(text(o))));
  }
  /** What to search for a skill: as written, then without a bracketed note ("Data Cataloging (Alation)"). */
  const skillTerms = (skill) => [...new Set([skill.trim(), skill.replace(/\s*\([^)]*\)\s*/g, " ").trim()])].filter(Boolean);
  /** Is this the "Type to Add Skills" box? Workday puts the cursor back in it after each pick: that is not you. */
  const isSkillsBox = (el) => !!(C && el && el.tagName === "INPUT" && C.skills.test(describe(el)));

  const skillOptions = () => [...document.querySelectorAll(C.skillOption)].filter(visible);
  const optionsKey = (opts) => opts.map((o) => norm(o.innerText)).join("\n");
  // Selected skills show as chips, in the field and in the open list (which Workday draws outside the field).
  const skillChips = () => [...document.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"] li')];
  const chipNamed = (name) => skillChips().some((c) => norm(c.innerText) === name || norm(c.innerText).startsWith(`${name} `));

  /** Type into the skills box the way a person does: no change / blur events (those close Workday's list). */
  function typeSkill(input, text) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function pressEnter(input) {
    for (const type of ["keydown", "keypress", "keyup"]) {
      input.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    }
  }

  /** This search's list, once it has replaced the list showing before the search (`before`, the last skill's) or a
   *  few seconds have passed, and looked the same twice in a row: { hit (the suggestion for `skill`, or null),
   *  listed (a list came at all) }. */
  async function settledSkillOption(skill, before) {
    let last = null, empty = 0;
    for (let i = 0; i < 30; i++) {  // Workday's search can take a few seconds
      await sleep(200);
      scripted = Date.now() + 1500;
      const opts = skillOptions();
      const key = optionsKey(opts);
      const gone = before.nodes.every((o) => !o.isConnected || !visible(o));
      const fresh = key !== before.key || gone || i >= 12;
      if (opts.length && fresh && key === last) return { hit: bestSkillOption(opts, skill), listed: true };
      // the last list went away and nothing came in its place: Workday has nothing for this search
      empty = !opts.length && before.nodes.length && gone ? empty + 1 : 0;
      if (empty >= 5) return { hit: null, listed: true };
      last = key;
    }
    return { hit: null, listed: false };
  }

  /** Is the suggestion named `name` selected? true / false when the page shows it (a chip, or the row's checkbox),
   *  null when it can't be told (then it is not clicked again, which could unselect it). */
  function skillSelected(name) {
    if (chipNamed(name)) return true;
    const row = skillOptions().map((o) => o.closest('[role="option"]') || o).find((r) => norm(r.innerText) === name);
    const box = row && row.querySelector('input[type="checkbox"], [role="checkbox"], [aria-checked]');
    if (!box) return null;
    return box.checked === true || box.getAttribute("aria-checked") === "true";
  }

  /** What to click on a suggestion: the row it was found by first, then its checkbox, its label, the row itself. */
  function optionTargets(o) {
    const row = o.closest('[role="option"]') || o;
    return [...new Set([o, row.querySelector('input[type="checkbox"], [role="checkbox"]'),
      row.querySelector('[data-automation-id="promptOption"]'), row].filter(Boolean))];
  }

  async function fillSkills() {
    const skills = ((setup.history || {}).skills || []).slice(0, MAX_SKILLS);
    const todo = skills.filter((s) => !skillsDone.has(norm(s)));
    if (!todo.length) return;
    const input = [...document.querySelectorAll(TEXT_SEL)]
      .find((el) => el.tagName === "INPUT" && visible(el) && !el.disabled && !el.readOnly && !touched.has(el) && isSkillsBox(el));
    if (!input) return;
    for (const skill of todo.slice(0, SKILLS_PER_TICK)) {
      if (touched.has(input)) break;  // you started typing in the box yourself
      if (chipNamed(norm(skill))) { skillsDone.add(norm(skill)); continue; }  // already added
      let hit = null, listed = false, term = skill;
      for (term of skillTerms(skill)) {
        const showing = skillOptions();
        const before = { key: optionsKey(showing), nodes: showing };
        scripted = Date.now() + 1500;
        typeSkill(input, term);
        pressEnter(input);
        ({ hit, listed } = await settledSkillOption(term, before));
        if (hit || !listed) break;
      }
      if (!hit) {
        typeSkill(input, "");
        const tries = (skillTries.get(norm(skill)) || 0) + 1;
        skillTries.set(norm(skill), tries);
        if (listed || tries >= 2) {  // Workday doesn't list it (or never answered): skipped
          skillsDone.add(norm(skill));
          if (listed) skillsMissing.push(skill);
        }
        continue;
      }
      skillsDone.add(norm(skill));
      const name = norm(hit.innerText);
      let picked = false;
      for (let attempt = 0; attempt < 3 && !picked; attempt++) {
        if (attempt) {  // the list was redrawn under the click: pick it again from the list as it is now
          hit = bestSkillOption(skillOptions(), term);
          if (!hit || norm(hit.innerText) !== name) break;
        }
        const targets = optionTargets(hit);
        scripted = Date.now() + 2500;  // Workday focuses the box again after a pick: not you
        if (!safeClick(targets[Math.min(attempt, targets.length - 1)], "option")) break;
        let state = null;
        for (let i = 0; i < 10; i++) {
          await sleep(150);
          state = skillSelected(name);
          if (state) break;
        }
        picked = state !== false;  // can't tell: trust the click rather than risk unselecting it
      }
      if (picked) { filled++; skillsAdded++; }
      await sleep(200);
    }
    scripted = Date.now() + 1500;
    if (input.value) typeSkill(input, "");  // leave nothing typed in the box
  }

  const inHistory = (el) => groupsMatching(C.exp.entry).concat(groupsMatching(C.edu.entry)).some((g) => g.contains(el));

  // ---------------------------------------------------------------- profile fields, dropdowns, radios, checkboxes
  const TEXT_SEL = 'input:not([type]), input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea';

  function fillTextFields(history) {
    for (const el of document.querySelectorAll(TEXT_SEL)) {
      if (!visible(el) || el.disabled || el.readOnly || el.value || touched.has(el) || history.some((g) => g.contains(el))) continue;
      const d = describe(el);
      if (/search/.test(d)) continue;
      for (const [key, re, not] of C.text) {
        if (re.test(d) && !(not && not.test(d)) && profile[key]) {
          setNativeValue(el, profile[key]);
          filled++;
          break;
        }
      }
    }
  }

  async function fillDropdowns(history, bank) {
    let done = 0;
    for (const btn of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
      if (done >= MAX_DROPDOWNS_PER_TICK) return;
      if (!visible(btn) || gaveUp.has(btn) || touched.has(btn) || history.some((g) => g.contains(btn))) continue;
      if (!placeholder(norm(btn.innerText))) continue;
      const d = describe(btn);
      const disclosure = disclosureOf(d);
      let wanted = null;
      if (disclosure) wanted = disclosureWanted(disclosure);
      else {
        const rule = ruleFor(labelOf(btn), d);
        if (rule) { wanted = rule[1]; btn.title = `Job Agent used your "${rule[3]}" setting (${rule[1]}) for this question.`; }
        else {
          const m = bank.get(labelOf(btn));
          if (m) wanted = [m.answer];
          if (m && await pickListbox(btn, wanted)) { markFromBank(btn, m, m.answer); done++; continue; }
          if (m) gaveUp.add(btn);
          continue;
        }
      }
      if (!wanted) continue;
      done++;
      if (await pickListbox(btn, wanted)) filled++; else gaveUp.add(btn);
    }
  }

  function radioGroups() {
    const groups = {};
    for (const r of document.querySelectorAll('input[type="radio"]')) {
      if (!visible(r) && !visible(r.parentElement)) continue;
      (groups[r.name] = groups[r.name] || []).push(r);
    }
    return Object.values(groups);
  }

  function fillRadios(bank) {
    for (const radios of radioGroups()) {
      if (radios.some((r) => r.checked || touched.has(r))) continue;
      const d = `${describe(radios[0])} | ${norm(groupLabel(radios[0]))}`;
      const disclosure = disclosureOf(d);
      let hit = null, m = null;
      if (disclosure) {
        const wanted = disclosureWanted(disclosure);
        if (wanted) hit = radios.find((r) => wanted.test(optionLabel(r)));
      } else {
        const rule = ruleFor(groupLabel(radios[0]), d);
        if (rule) {
          const want = norm(rule[1]);
          hit = radios.find((r) => optionLabel(r) === want) || radios.find((r) => optionLabel(r).startsWith(`${want} `));
        }
        else if ((m = bank.get(groupLabel(radios[0])))) hit = radios.find((r) => optionLabel(r) === norm(m.answer));
      }
      if (hit && safeClick(hit, "radio")) {
        if (m) markFromBank(hit.closest("fieldset, [role='radiogroup']") || hit.parentElement, m, m.answer); else filled++;
      }
    }
  }

  /** Disability self-identification and similar: tick "I do not want to answer" when that is your choice.
   *  Only a checkbox whose own label says so, in a group about a disclosure; consent boxes are never ticked. */
  function fillDisclosureCheckboxes() {
    const boxes = new Map();
    for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
      const box = cb.closest('fieldset, [role="group"], [data-automation-id^="formField"]') || cb.parentElement;
      (boxes.get(box) || boxes.set(box, []).get(box)).push(cb);
    }
    for (const [box, cbs] of boxes) {
      if (cbs.some((c) => c.checked || touched.has(c))) continue;
      const kind = disclosureOf(norm(`${groupLabel(cbs[0])} ${box.getAttribute("data-automation-id") || ""} ${headingOf(box)}`));
      const wanted = disclosureWanted(kind);
      if (!wanted) continue;
      const hit = cbs.find((c) => wanted.test(optionLabel(c)));
      if (hit && safeClick(hit, "checkbox")) filled++;
    }
  }

  // ---------------------------------------------------------------- answer bank
  /** Custom questions on the page that are still empty: [{ el, q, type }]. */
  function customQuestions(history) {
    const out = [];
    for (const el of document.querySelectorAll(TEXT_SEL)) {
      if (!visible(el) || el.disabled || el.readOnly || el.value || touched.has(el) || history.some((g) => g.contains(el))) continue;
      const d = describe(el);
      if (/search|password/.test(d) || isStandard(el, d)) continue;
      const q = labelOf(el);
      if (q.length >= 8) out.push({ el, q, type: "text" });
    }
    for (const btn of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
      if (!visible(btn) || touched.has(btn) || !placeholder(norm(btn.innerText)) || history.some((g) => g.contains(btn)) || isStandard(btn)) continue;
      const q = labelOf(btn);
      if (q.length >= 8) out.push({ el: btn, q, type: "listbox" });
    }
    for (const radios of radioGroups()) {
      if (radios.some((r) => r.checked || touched.has(r))) continue;
      const q = groupLabel(radios[0]);
      if (q.length >= 8 && !isStandard(radios[0], `${describe(radios[0])} | ${norm(q)}`)) out.push({ el: radios[0], q, type: "radio" });
    }
    return out;
  }

  /** question -> saved answer, asking Job Agent only about questions it hasn't been asked yet. */
  async function bankAnswers(questions) {
    if (!setup.options?.answers) return new Map();
    const fresh = [...new Set(questions.map((x) => x.q))].filter((q) => !asked.has(q));
    if (fresh.length) {
      const res = (await event("answers", fresh)) || [];
      fresh.forEach((q, i) => asked.set(q, res[i] || null));
    }
    return new Map(questions.map((x) => [x.q, asked.get(x.q)]).filter(([, m]) => m));
  }

  function fillTextFromBank(questions, bank) {
    for (const { el, q, type } of questions) {
      const m = bank.get(q);
      if (type !== "text" || !m || el.value || touched.has(el)) continue;
      setNativeValue(el, m.answer);
      markFromBank(el, m, m.answer);
    }
  }

  // Remember what you type or pick in a custom question (only your own input: isTrusted events).
  function capture(question, answer, kind) {
    if (!setup?.options?.capture) return;
    const q = cleanLabel(question), a = (answer || "").trim();
    if (q.length < 8 || !a || placeholder(norm(a)) || sentAnswers.get(q) === a) return;
    sentAnswers.set(q, a);
    event("answer", { question: q, answer: a, kind });
  }

  function onUserInput(e) {
    if (!e.isTrusted || !C) return;
    const el = e.target;
    if (!el || !el.matches) return;
    if (el.matches('input[type="radio"]') && e.type === "change") {
      const d = `${describe(el)} | ${norm(groupLabel(el))}`;
      if (!isStandard(el, d) && !inHistory(el)) capture(groupLabel(el), optionLabel(el) && labelText(el), "choice");
      return;
    }
    if (!el.matches(TEXT_SEL) || el.type === "password" || inHistory(el)) return;
    const d = describe(el);
    if (/search|password/.test(d) || isStandard(el, d)) return;
    const value = el.value.trim();
    if (bankFilled.has(el) && bankFilled.get(el) === value) return; // the bank's answer, unchanged
    capture(labelOf(el), value, "text");
  }
  const labelText = (input) => {
    const l = input.id && document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
    return ((l && l.innerText) || input.getAttribute("aria-label") || input.value || "").trim();
  };

  function onUserClick(e) {
    if (!e.isTrusted || !C) return;
    const btn = e.target.closest?.('button[aria-haspopup="listbox"]');
    if (btn) { lastListbox = btn; return; }
    const opt = e.target.closest?.('[role="option"]');
    if (opt && lastListbox) {
      const b = lastListbox;
      setTimeout(() => {
        if (!document.contains(b) || isStandard(b) || inHistory(b)) return;
        const value = (b.innerText || "").trim();
        if (bankFilled.has(b) && bankFilled.get(b) === value) return;
        capture(labelOf(b), value, "choice");
      }, 400);
    }
  }
  // Anything you focus, type in or click is yours from then on: autofill won't fill it again (not even when you
  // empty it to retype). Script-made events (isTrusted false) and the banner's own focus() don't count.
  function markTouched(e) {
    if (!e.isTrusted || (e.type === "focusin" && Date.now() < scripted)) return;
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    if (e.type === "focusin" && isSkillsBox(el)) return;  // Workday puts the cursor back there after each pick
    touched.add(el);
    const btn = el.closest?.('button[aria-haspopup="listbox"]');
    if (btn) touched.add(btn);
  }
  for (const type of ["focusin", "input", "keydown", "change", "click"]) document.addEventListener(type, markTouched, true);
  document.addEventListener("focusout", onUserInput, true);
  document.addEventListener("change", onUserInput, true);
  document.addEventListener("click", onUserClick, true);

  // ---------------------------------------------------------------- what is still missing
  function isRequired(el, raw) {
    return el.required || el.getAttribute("aria-required") === "true" || /\*/.test(raw);
  }

  /** Required fields on this page that are still empty: [{ el, label }]. */
  function missingRequired() {
    const out = [];
    const radiosDone = new Set();
    for (const el of document.querySelectorAll('input, textarea, button[aria-haspopup="listbox"]')) {
      if (["hidden", "file", "password", "submit", "button"].includes(el.type) && el.tagName !== "BUTTON") continue;
      if (el.tagName === "BUTTON" && el.getAttribute("aria-haspopup") !== "listbox") continue;
      if (el.type === "radio") {
        if (radiosDone.has(el.name)) continue;
        radiosDone.add(el.name);
        const group = [...document.querySelectorAll(`input[type="radio"][name="${CSS.escape(el.name)}"]`)];
        const raw = groupLabel(el, true);
        if (!visible(el) && !visible(el.parentElement)) continue;
        if ((group.some((r) => r.required || r.getAttribute("aria-required") === "true") || /\*/.test(raw)) && !group.some((r) => r.checked)) {
          out.push({ el, label: cleanLabel(raw) || el.name });
        }
        continue;
      }
      if (el.type === "checkbox") continue; // consent boxes are yours to tick; Workday will point them out
      if (!visible(el) || el.disabled) continue;
      const raw = labelOf(el, true);
      if (!isRequired(el, raw)) continue;
      const empty = el.tagName === "BUTTON" ? placeholder(norm(el.innerText)) : !el.value.trim();
      if (empty) out.push({ el, label: cleanLabel(raw) || el.getAttribute("aria-label") || el.name || "a field" });
    }
    return out;
  }

  // ---------------------------------------------------------------- banner
  // Built with DOM calls and element.style (never innerHTML or style="" markup), so a site's Content-Security-Policy
  // or Trusted Types rules can't block or strip it.
  function node(tag, css, text) {
    const n = document.createElement(tag);
    n.style.cssText = css;
    if (text) n.textContent = text;
    return n;
  }

  function banner(msg, missing = []) {
    let b = document.getElementById("__jobagent_banner");
    if (!b) {
      b = node("div", "position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;background:#0f172a;color:#f8fafc;" +
        "font:13px/1.4 system-ui,Segoe UI,sans-serif;padding:12px 14px;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.35)");
      b.id = "__jobagent_banner";
      const text = node("div", "");
      text.id = "__jobagent_msg";
      const list = node("div", "margin-top:6px");
      list.id = "__jobagent_missing";
      const done = node("button", "margin-top:8px;background:#22c55e;color:#04210f;border:0;border-radius:6px;padding:6px 10px;" +
        "font-weight:600;cursor:pointer", "I submitted â€” mark as applied");
      done.id = "__jobagent_done";
      done.onclick = () => markApplied("manual");
      b.append(node("div", "font-weight:600;margin-bottom:4px", "Job Agent autofill"), text, list, done);
      document.documentElement.appendChild(b);
    }
    b.querySelector("#__jobagent_msg").textContent = msg;
    const list = b.querySelector("#__jobagent_missing");
    const key = missing.map((m) => m.label).join("|");
    if (list.dataset.key === key) return;
    list.dataset.key = key;
    list.replaceChildren();
    if (!missing.length) return;
    list.append(node("div", "font-weight:600;color:#fcd34d", `Still to fill on this page (${missing.length}):`));
    for (const m of missing.slice(0, 8)) {
      const item = node("button", "display:block;background:none;border:0;padding:1px 0;color:#bfdbfe;cursor:pointer;" +
        "text-align:left;font:inherit;text-decoration:underline", `â€¢ ${m.label.slice(0, 70)}`);
      item.onclick = () => { scripted = Date.now() + 300; m.el.scrollIntoView({ block: "center" }); m.el.focus?.(); };
      list.append(item);
    }
    if (missing.length > 8) list.append(node("div", "color:#cbd5e1", `â€¦and ${missing.length - 8} more`));
  }

  function markApplied(how) {
    if (reportedApplied) return;
    reportedApplied = true;
    event("applied", how);
    banner("Marked as applied. The application folder has been updated.");
  }

  // ---------------------------------------------------------------- main loop
  async function tick() {
    if (busy) return;
    busy = true;
    try {
      if (!setup) {
        setup = await event("setup");
        if (!setup || !setup.rules) { setup = null; return; }
        C = compile(setup.rules);
        profile = setup.profile || {};
      }
      const passwords = [...document.querySelectorAll('input[type="password"]')].filter(visible);
      if (passwords.length) {
        banner(!profile.account_ready
          ? "Add WORKDAY_EMAIL and WORKDAY_PASSWORD to the .env file in your Job Agent folder to have this form filled. For now, sign in or create the account yourself."
          : passwords.length >= 2
            ? "Typed your Workday email and password from .env. Tick the terms box if there is one, then click Create Account yourself."
            : "Typed your Workday email and password from .env. Click Sign In yourself (or Create Account if you have no account here yet).");
        return;
      }
      const text = document.body ? document.body.innerText : "";
      if (C.submitted.test(text)) {
        markApplied("detected");
        return;
      }
      const exp = await fillHistory("exp");
      const edu = await fillHistory("edu");
      const history = groupsMatching(C.exp.entry).concat(groupsMatching(C.edu.entry));
      fillTextFields(history);
      await fillSkills();
      const questions = customQuestions(history);
      const bank = questions.length ? await bankAnswers(questions) : new Map();
      fillTextFromBank(questions, bank);
      fillRadios(bank);
      fillDisclosureCheckboxes();
      await fillDropdowns(history, bank);
      if (!reportedApplied) {
        const notes = [`Filled ${filled} field(s) and attached your files when asked.`];
        if (fromBank) notes.push(`${fromBank} answer(s) came from your answer bank: they are outlined amber, check them.`);
        const short = [["job", exp], ["school", edu]].filter(([, r]) => r.wanted > r.have);
        if (short.length && !setup.options?.add_entries) {
          notes.push(`Your resume has ${short.map(([w, r]) => `${r.wanted} ${w}${r.wanted > 1 ? "s" : ""}`).join(" and ")}; ` +
            "click Add for each (or turn on Settings â†’ Autofill â†’ Add entries).");
        }
        const skillCount = ((setup.history || {}).skills || []).length;
        if (skillsDone.size) {
          const more = skillsMissing.length > 6 ? "…" : "";
          notes.push(`Skills: added ${skillsAdded} of ${Math.min(skillCount, MAX_SKILLS)}${skillsMissing.length
            ? `; Workday doesn't list ${skillsMissing.slice(0, 6).join(", ")}${more}` : ""}.`);
        }
        notes.push("Check every step, then click Submit yourself.");
        banner(notes.join(" "), missingRequired());
      }
    } catch (e) {
      console.warn("Job Agent autofill:", e);
    } finally {
      busy = false;
    }
  }

  setInterval(tick, 1500);
})();
