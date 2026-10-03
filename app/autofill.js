// Injected into Workday application pages opened by Job Agent.
// Fills standard fields from your profile. It never clicks Create Account, Sign In, Next or Submit, and never
// overwrites something you already typed. You review and submit.
// (The Create Account / Sign In email + password come from your .env file and are typed by the Python side,
//  so the password never passes through this script.)
(() => {
  if (window.__jobAgentLoaded) return;
  window.__jobAgentLoaded = true;
  if (!/myworkday(jobs|site)\.com$/i.test(location.hostname)) return;

  const STATE_NAMES = {AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",CO:"Colorado",CT:"Connecticut",DE:"Delaware",DC:"District of Columbia",FL:"Florida",GA:"Georgia",HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",KY:"Kentucky",LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",MT:"Montana",NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",NY:"New York",NC:"North Carolina",ND:"North Dakota",OH:"Ohio",OK:"Oklahoma",OR:"Oregon",PA:"Pennsylvania",PR:"Puerto Rico",RI:"Rhode Island",SC:"South Carolina",SD:"South Dakota",TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",VA:"Virginia",WA:"Washington",WV:"West Virginia",WI:"Wisconsin",WY:"Wyoming"};

  let profile = null;
  let filled = 0;
  let busy = false;
  let reportedApplied = false;
  const gaveUp = new WeakSet();
  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
  const visible = (el) => !!(el && el.offsetParent !== null && el.getClientRects().length);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function describe(el) {
    const parts = [el.id, el.name, el.getAttribute("data-automation-id"), el.getAttribute("aria-label"), el.placeholder];
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) parts.push(l.innerText);
    }
    (el.getAttribute("aria-labelledby") || "").split(/\s+/).forEach((id) => {
      const n = id && document.getElementById(id);
      if (n) parts.push(n.innerText);
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

  // [profile key, label pattern, exclusion pattern]
  const TEXT_RULES = [
    ["first_name", /first ?name|given ?name|firstname/, /preferred|middle|local/],
    ["last_name", /last ?name|family ?name|surname|lastname/, /preferred|local/],
    ["email", /e-?mail/, /confirm|verify|retype/],
    ["phone", /phone ?number|phonenumber|mobile number|telephone|\bphone\b/, /extension|country|code|device|type/],
    ["address1", /address ?line ?1|addressline1|street address|address 1|address--addressline1/, /e-?mail/],
    ["address2", /address ?line ?2|addressline2|address 2/, /e-?mail/],
    ["city", /\bcity\b|town/, /ethnic|citizen/],
    ["postal_code", /postal|zip/, null],
    ["linkedin", /linkedin/, null],
    ["github", /github/, null],
    ["website", /website|portfolio|personal url|blog/, /linkedin|github/],
  ];

  function choiceRules() {
    return [
      [/how did you hear|referral source|\bsource\b/, profile.how_heard],
      [/phone device type|device type/, profile.phone_type],
      [/authori[sz]ed to work|legally (authorized|eligible|permitted)|eligible to work|right to work/, profile.authorized_us],
      [/sponsor/, profile.needs_sponsorship],
      [/previously (worked|been employed)|former (employee|worker)|ever (worked|been employed)|have you worked for|previousworker/, profile.previously_employed],
      [/\bstate\b|region|province/, STATE_NAMES[(profile.state || "").toUpperCase()] || profile.state],
      [/\bcountry\b/, profile.country],
    ];
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function fillTextFields() {
    const sel = 'input:not([type]), input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea';
    for (const el of document.querySelectorAll(sel)) {
      if (!visible(el) || el.disabled || el.readOnly || el.value) continue;
      const d = describe(el);
      if (/search/.test(d)) continue;
      for (const [key, re, not] of TEXT_RULES) {
        if (re.test(d) && !(not && not.test(d)) && profile[key]) {
          setNativeValue(el, profile[key]);
          filled++;
          break;
        }
      }
    }
  }

  async function pickListbox(button, wanted) {
    button.click();
    for (let i = 0; i < 15; i++) {
      await sleep(150);
      const opts = [...document.querySelectorAll('[role="option"]')].filter(visible);
      if (!opts.length) continue;
      const w = norm(wanted);
      const hit = opts.find((o) => norm(o.innerText) === w) || opts.find((o) => norm(o.innerText).startsWith(w));
      if (hit) {
        hit.click();
        filled++;
        return true;
      }
      break;
    }
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.body.click();
    return false;
  }

  async function fillDropdowns() {
    for (const btn of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
      if (!visible(btn) || gaveUp.has(btn)) continue;
      const current = norm(btn.innerText);
      if (current && !/^select one|^select$|^choose/.test(current)) continue;
      const d = describe(btn);
      const rule = choiceRules().find(([re, v]) => v && re.test(d));
      if (!rule) continue;
      const ok = await pickListbox(btn, rule[1]);
      if (!ok) gaveUp.add(btn);
      return; // one dropdown per tick keeps the page calm
    }
  }

  function fillRadios() {
    const groups = {};
    for (const r of document.querySelectorAll('input[type="radio"]')) {
      if (!visible(r) && !visible(r.parentElement)) continue;
      (groups[r.name] = groups[r.name] || []).push(r);
    }
    for (const radios of Object.values(groups)) {
      if (radios.some((r) => r.checked)) continue;
      const d = describe(radios[0]);
      const rule = choiceRules().find(([re, v]) => v && re.test(d));
      if (!rule) continue;
      const want = norm(rule[1]);
      const hit = radios.find((r) => {
        const l = r.id && document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
        return norm((l && l.innerText) || r.value) === want;
      });
      if (hit) {
        hit.click();
        filled++;
      }
    }
  }

  // Built with DOM calls and element.style (never innerHTML or style="" markup), so a site's Content-Security-Policy
  // or Trusted Types rules can't block or strip it.
  function node(tag, css, text) {
    const n = document.createElement(tag);
    n.style.cssText = css;
    if (text) n.textContent = text;
    return n;
  }

  function banner(msg) {
    let b = document.getElementById("__jobagent_banner");
    if (!b) {
      b = node("div", "position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:340px;background:#0f172a;color:#f8fafc;" +
        "font:13px/1.4 system-ui,Segoe UI,sans-serif;padding:12px 14px;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.35)");
      b.id = "__jobagent_banner";
      const text = node("div", "");
      text.id = "__jobagent_msg";
      const done = node("button", "margin-top:8px;background:#22c55e;color:#04210f;border:0;border-radius:6px;padding:6px 10px;" +
        "font-weight:600;cursor:pointer", "I submitted — mark as applied");
      done.id = "__jobagent_done";
      done.onclick = () => markApplied("manual");
      b.append(node("div", "font-weight:600;margin-bottom:4px", "Job Agent autofill"), text, done);
      document.documentElement.appendChild(b);
    }
    b.querySelector("#__jobagent_msg").textContent = msg;
  }

  function markApplied(how) {
    if (reportedApplied) return;
    reportedApplied = true;
    window.__jobAgentEvent && window.__jobAgentEvent("applied", how);
    banner("Marked as applied. The application folder has been updated.");
  }

  async function tick() {
    if (busy) return;
    busy = true;
    try {
      if (!profile && window.__jobAgentEvent) profile = await window.__jobAgentEvent("profile");
      if (!profile) return;
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
      if (/application (has been )?submitted|thank you for applying|successfully submitted/i.test(text)) {
        markApplied("detected");
        return;
      }
      fillTextFields();
      fillRadios();
      await fillDropdowns();
      if (!reportedApplied) {
        banner(`Filled ${filled} field(s) and attached your tailored resume when asked. ` +
          "Check every step, finish anything left blank, then click Submit yourself.");
      }
    } catch (e) {
      console.warn("Job Agent autofill:", e);
    } finally {
      busy = false;
    }
  }

  setInterval(tick, 1500);
})();
