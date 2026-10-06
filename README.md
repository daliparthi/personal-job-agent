# Job Agent — Workday (+ Greenhouse, Lever, Ashby, SmartRecruiters) job search + local AI resume tailoring

> **⚠️ Use at your own risk.** A master resume / CV contains personally identifiable information, so this tool
> could **not** be dry-run with a real resume — it was tested only with fictional sample resumes such as
> `samples/sample_master_resume.txt`. The tailored resume is a **starting point for further editing**, not a
> finished document. Read every line before you send it anywhere.

Job Agent runs on your own computer. It searches the Workday career sites (and Greenhouse, Lever, Ashby and
SmartRecruiters job boards) you list in `companies.yaml`, scores every posting against your master resume, and — when you ask — rewrites a copy of your resume for one posting
at a time with **Qwen2.5‑0.5B running inside your browser** (no cloud AI service, no API keys). It can then open the
posting, fill the standard Workday fields (and the Create Account / Sign In form from your `.env`), and save everything
to an `Applications/<Company>/` folder.

---

## Quick start (Windows, macOS, Linux)

Get the code:

```
git clone --depth 1 https://github.com/daliparthi/personal-job-agent.git
cd personal-job-agent
```

(or download the ZIP from GitHub and unzip it). The download is about 1.1 GB because the AI model files are
included, so nothing has to be downloaded when you start it. You need **Python 3.10 or newer** and **Chrome, Edge or Chromium** (Chrome/Edge recommended for the app; one of the
three is needed for *Apply with autofill* and PDF export).

| | Start Job Agent | Install Python if needed |
|---|---|---|
| **Windows** | double-click **`start.bat`** | [python.org](https://www.python.org/downloads/) (the `py` launcher is used if present) |
| **macOS** | double-click **`start.command`** in Finder (or run `./start.sh` in Terminal) | [python.org](https://www.python.org/downloads/) or `brew install python` — the built-in 3.9 is too old |
| **Linux** | run **`./start.sh`** in a terminal | `sudo apt install python3 python3-venv` / `sudo dnf install python3` |

The first run creates a Python environment for that computer (`.venv` on Windows, `.venv-darwin` / `.venv-linux`
elsewhere, so one folder can be shared between computers) and installs the packages; it reinstalls them whenever
`requirements.txt` changes. The Qwen2.5‑0.5B model files are already in `models/` (if you delete them, the start
script downloads them again once). Your browser opens Job Agent on
`http://localhost:8765/?key=…` (the key is yours, see [Several people on one computer](#several-people-on-one-computer)).

Then:

1. **Upload master resume** (DOCX, PDF, TXT or MD). The local model builds **`master_resume.yaml`** from it; review it
   and click **Save master resume**.
2. **Settings** → enter your *current employer* (it is never searched or shown) and your applicant profile.
   Click **Edit .env** and put your Workday email and password in it (optional, see [Applying](#applying)).
3. Type **mandatory** keywords (and optional ones), click **Run job search**.
4. Pick a position → **Tailor my resume for this position only** → approve/reject each missing keyword → watch the
   resume being edited live → hover any changed line to **Undo** it → optionally **Write cover letter** →
   **Save package** or **Apply with autofill**.

Manual start: `<environment>/bin/python run.py` (Windows: `.venv\Scripts\python run.py`), options below.

---

## Your personal folder

The project folder holds only code and model files. Everything personal lives in a folder of your own, inside your
home folder:

```
<home>/JobAgent/default/     Windows C:\Users\you\JobAgent\default · macOS /Users/you/JobAgent/default
                             Linux /home/you/JobAgent/default
  .env                 Workday account email (+ password unless it is in your OS keychain; only typed into
                       Workday's sign-up / sign-in form)
  master_resume.yaml   your master resume as data (built by the model, edit freely)
  my_companies.yaml    your own career sites on top of the shared list (Settings > Add company writes here)
  jobs.db              settings, profile, postings (new ones from the last 5 days; jobs you worked on are kept), tailored copies
  browser-profile/     the apply window's browser profile (your Workday logins)
  Applications/        one folder per company with what you sent
  Alerts/              daily pages of new matches found by scheduled searches
```

Settings → *Your personal folder* shows the path and opens it. On Windows it is outside OneDrive's Documents folder
on purpose; on macOS and Linux the folder is made readable only by you (`chmod 700`, `.env` `600`).

### Several people on one computer

* **Each user account** automatically gets their own folder in their own home folder.
* **Several people on one account**: `.\start.bat --profile Alex` (Windows) / `./start.sh --profile Alex`
  (macOS, Linux) uses `<home>/JobAgent/Alex`. (Double-clicking takes no options; type the command in a terminal.)
* **Somewhere else**: `--home "D:\Job data"` / `--home ~/job-data`.

(Windows Command Prompt: type `.\start.bat`, not `start.bat` — the bare name collides with its built-in `start`
command. Double-clicking is unaffected.)

Each person's Job Agent runs on its own port (8765, 8766, …, picked automatically) and only answers a browser that was
opened through that person's start link (a per-person key stored in their folder), so one person cannot open another
person's resume by browsing to `localhost`. Starting the same profile twice simply reopens the running one. Other
options: `--port 8800`, `--no-browser`.

---

## What it does

| Requirement | How it works |
|---|---|
| Search Workday sites of top companies | Uses each site's public Workday JSON API (the same calls the careers page makes). Sites come from the shared `companies.yaml` plus your own `my_companies.yaml`, re-read for every search. |
| Other job boards | Greenhouse, Lever, Ashby and SmartRecruiters boards go in the same lists. Each publishes its postings as public JSON: Greenhouse, Lever and Ashby send the whole board in one request, which Job Agent filters with the same keyword, US-location and date rules as a Workday search; SmartRecruiters is searched with your keywords and limited to US postings. Every posting gets the same fields (job type, remote, state, salary, match score) whatever its source, and a grey badge names the board. |
| Mandatory keywords | Every mandatory keyword/phrase must appear in the title or description, or the posting is dropped. Comma-separated. |
| Optional keywords | Can be blank. They never exclude anything by default; they are highlighted (blue), shown as badges, and you can tick *Must match an optional keyword* to refine. If you leave mandatory blank, each optional keyword is searched separately. |
| First run = last 5 days | The first run for a keyword set looks back 5 days. |
| Incremental by default | Later runs only pull postings newer than that company's last successful run, and skip jobs already stored. |
| Full refresh flag | Tick **Full refresh** to ignore the incremental cursor and re-pull/re-parse the whole 5-day window. |
| New keywords | Changing mandatory keywords starts a fresh 5-day pull for the new set (the cursor is per keyword set). |
| DB purge | `jobs.db` in your personal folder; untouched postings (status *new*, no notes or follow-up) posted more than 5 days ago (Settings → *Keep untouched postings for*) are deleted on start-up, before every search, and hourly. `jobs.db` is also kept under 50 MB (shown in the top bar): if it is still over after that, the oldest untouched postings go first, then run logs, then the raw HTML of old postings you worked on (their text stays). Jobs you tailored, saved, applied to or moved along the [pipeline](#pipeline) are kept forever, and saved application folders are never deleted. |
| Skip current employer | Settings → *Current employer*. That company is skipped during search and hidden from results. |
| USA only + state filters | Searches use each site's "United States" filter when it has one, then every posting is checked for a US location. Filter by **state** (multi-select) and **city**. Remote roles with no state stay visible when you pick states. |
| Contract / Full-time / Temporary | From Workday's *Job Type* (worker sub-type) facet, falling back to the posting text. Colour badges: **Full-time** green, **Contract** amber, **Temporary** purple, **Part-time** blue, Internship grey. Click a type chip to omit it; ✕ on a card hides one posting. |
| Salary filter | Parsed from pay-transparency text (`$120,000 - $150,000`, `200,000 USD - 322,000 USD`, `$45/hr` → annualised). Set *Min salary*; choose whether to include postings without a salary. |
| Remote only | Tick **Remote only** (uses Workday's remote type, location names and posting text). |
| Master resume as YAML | On upload the local model builds `master_resume.yaml` (see below). Tailoring always works on a copy; the master changes only when you upload again or edit it. |
| Match score before tailoring | ATS-style score (0–100) of your **master** resume vs each posting. |
| Sort | By match score (high → low), then salary (high → low). |
| Side-by-side view | Resume (Master / Tailored tabs) next to the job description, with keywords highlighted: green = already in your resume, red = missing. |
| Tailor for one position | Only the selected posting. Runs Qwen2.5-0.5B locally and streams the edit **word by word** into the resume. |
| Missing keywords | Before tailoring, a pop-up walks through each missing keyword with where the JD uses it — **Approve** (you really have it) or **Reject**. Only approved keywords can be added. |
| Undo each change | Hover any line the tailoring changed: **↶ Undo** appears at its end (then **↷ Redo**). Lines the AI wrote but held back offer **Use AI version**. |
| Cover letter + short answers | **Write cover letter** (after tailoring) drafts a letter and answers to three common application questions from your tailored resume, with the same checks and undo as the resume. See [Cover letters and short answers](#cover-letters-and-short-answers). |
| Help apply | Opens the posting in a separate Chrome, Edge or Chromium window, clicks the posting's *Apply* button, fills Workday's Create Account / Sign In form from `.env`, uploads the tailored resume (and the cover letter where a field asks for one) and fills standard fields. **You click Create Account, Sign In and Submit.** |
| Applied jobs folder | `Applications/<Company>/<Job title - ReqID>/` in your personal folder: the tailored resume (DOCX, PDF, TXT, `tailored_resume.yaml`), the cover letter (DOCX, PDF, TXT) and `Short_Answers.txt` when you wrote them, the job description (HTML, TXT) and `application.json`. |

---

## master_resume.yaml

When you upload a resume:

1. Job Agent reads the file and makes a rule-based first draft (sections, jobs, bullets, dates, email/phone/links).
2. **The local model** reads the top of your resume (name, headline, location) and every job and education heading,
   and splits them into fields — `title`, `company`, `location`, `degree`, `school`. You watch the YAML fill in live.
3. **Bullets and the summary are copied word for word** — the model never retypes them. Every value the model returns
   must appear in the text it came from (otherwise the rule-based guess is kept), so it cannot invent anything; if
   its fields would drop words from a heading, the heading is kept exactly as written.
4. You review the YAML (and can edit it), then **Save master resume**.

Later: **Master tab → Edit YAML**, or edit the file in any text editor — Job Agent notices and re-scores. A YAML
mistake is reported with its line number and the last good copy stays in use. On a CPU the build takes about 30–40
seconds for a two-page resume; **Skip AI, use quick parse** uses the rule-based draft only.

```yaml
name: Jordan Avery
headline: Senior Data Engineer
contact: {email: …, phone: …, location: Austin, TX, links: [...]}
# links: "linkedin.com/in/you" prints as a clickable "LinkedIn" (GitHub likewise) in the DOCX / PDF;
# for your own wording use  links: [{text: Portfolio, url: https://example.com}]
sections:
  - title: Professional Experience
    kind: experience           # summary, skills, experience, projects, education, certifications, other
    entries:
      - title: Senior Data Engineer
        company: Northwind Analytics
        location: Austin, TX
        start: Mar 2021
        end: Present
        bullets:
          - Designed Spark and Airflow pipelines that ...
```

## The match score

```
score = 60% keyword coverage   (weighted ATS keywords in the JD that your resume contains)
      + 25% wording similarity  (cosine similarity of the two texts)
      + 15% title alignment     (words of the job title found in your resume)
```

Keywords come from `app/lexicon.py` (≈450 skills, tools, certifications and soft skills — add your own lines), repeated
acronyms in the posting. Your search keywords do not change the score, so one resume and one posting always give the same number (the years of experience count up to the day you last saved the resume, not to today). The employer's own name is never counted as a skill. After tailoring
you see **before → after** (red when an edit lowered it — undo lines until it goes back up).

**Where a keyword appears matters.** The posting is split by its headings: keywords under its required
qualifications count ×1.5, in the duties or overview ×1, under *preferred / nice to have* ×0.75, and not at all
when they only appear in the parts about the company, pay, benefits and equal opportunity (often a third of a
posting). Missing chips marked <sup>req</sup> are required; faded ones are only nice to have.

**Then the score is adjusted, always with a reason** (the *Why* box at the top of the job description):

| | |
|---|---|
| Years of experience | The years the posting asks for vs. the years your jobs' dates add up to (overlaps counted once): −3 per missing year, at most −15. "5+ years of Kafka" is checked against the jobs whose lines mention Kafka (−2 each). |
| Seniority | A role two or more levels above your latest title (e.g. a director role for a senior engineer): −8; two or more below: −3. |
| Hard requirements | No visa sponsorship (when you need it), US citizens only, a security clearance (when your profile says you have none), or a degree above yours: −10 each (at most −20), shown as ⛔ in the list. *Hide ones I don't qualify for* filters them out. Fill in *US citizen?* / *Security clearance?* in Settings → Applicant profile; left blank they only warn. |

**Sponsorship, H-1B and H-4 EAD.** Each posting is also read for what it says about work authorization: green badges
(*sponsors visas*, *H-1B*, *H-4 EAD ok*) in the list, and a **Sponsorship** filter: *Sponsors visas / H-1B*, *Accepts
H-4 EAD*, or *Not "no sponsorship"* (drops postings that say they won't sponsor). A posting that says nothing is never
tagged, so *Not "no sponsorship"* keeps it.

The *Why* box also lists each requirement line of the posting with ✓ / ◐ / ✗ and the line of your resume that shows
it. 👍 / 👎 record whether a match was good; `tools/calibrate.py` uses those (and how far your applications got on
the Pipeline) to check whether another 60/25/15 split would rank your good matches higher.

## Tailoring, checks and undo

A 0.5B model is small, so every line it writes is checked:

* **Changes:** professional summary, up to *N* experience/project bullets (Settings → *Max bullets*, default 12),
  ordering of skills (job-relevant first), plus an `Additional:` skills line with approved keywords not used elsewhere.
* **Never changes:** name/contact, employers, titles, dates, education — and never `master_resume.yaml` itself.
* **Applied (green):** the new wording, with new words highlighted and approved keywords underlined.
* **Applied, check this (amber bar):** reworded heavily, dropped a skill the line had, added words that aren't in your
  resume, or moved a tool ("migrated *to* Snowflake" no longer says *to Snowflake*). Hover to see why.
* **Held back (dashed bar):** the AI line added a fact you didn't approve — a new number, a tool or company name your
  resume doesn't mention, or a keyword you rejected. Your original stays; hover → **Use AI version** if it is true.
* **Dropped:** garbled output (repeating the instructions, repeating itself, copying the prompt's example) is discarded.
* **Undo / Redo:** hover any changed line; the control appears at the end of the line. Removed lines (such as an undone
  `Additional:` line) stay visible struck through so you can redo them. Click any line to edit it yourself — your edit
  can be undone too. Everything is saved and re-scored as you go.

## Cover letters and short answers

**Write cover letter** (resume pane, after tailoring) opens the **Cover letter** tab and writes:

* **Opening and closing** from fixed templates: the position and company, your summary's first sentence as an
  introduction ("I am a data engineer with 8 years of …"), and up to three skills your resume and the posting share.
* **One or two body paragraphs**, each about the job or project whose bullets best match the posting. Each starts as
  a plain paragraph made only of your bullets ("As Senior Data Engineer at Northwind, I designed … I also migrated …");
  the AI then rewrites it.
* **Short answers** to *Why are you interested in this role?*, *Why do you want to work at …?* and *What relevant
  experience do you have?*, written from your bullets and the posting's own words about the company and the role.
  Make the "why this company" one personal. **Copy** puts an answer on the clipboard.

The AI versions get the same checks as tailored resume lines: a new number, a tool or company your resume doesn't
mention, or a keyword you didn't approve keeps the plain version and offers **Use AI version**; an unfinished or
garbled reply is dropped; a paragraph that adds more than a few words of its own is marked amber. Undo, redo and
editing work as in the tailored resume, and everything is saved with the tailored resume.

**Save package** then also writes `<First>_<Last>_Cover_Letter.docx` / `.pdf` / `.txt` and `Short_Answers.txt`, and
the apply window attaches the cover letter when a Workday upload field is labelled *Cover letter* (a field labelled
*Resume/CV*, or an unlabelled one, gets the resume as before).

## The AI engine: CPU by default, GPU when available

The model files are included in `models/` and served from `localhost`; nothing is downloaded while you use the app.
The 488 MB CPU model is stored as `model_quantized.onnx.part1`–`.part6` (GitHub refuses files over 100 MB) and the
app joins the parts as it serves the file. Licenses: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

| Engine | Folder | Size | Used when |
|---|---|---|---|
| **CPU** (ONNX via WebAssembly, several threads) | `models/onnx/Qwen2.5-0.5B-Instruct` | 500 MB | the default; any browser |
| GPU (WebLLM via WebGPU) | `models/webllm/Qwen2.5-0.5B-Instruct-q4f16_1-MLC` | 290 MB | the browser has a real GPU with `shader-f16` (e.g. Intel Iris Xe in Chrome/Edge) |
| GPU (WebLLM via WebGPU) | `models/webllm/Qwen2.5-0.5B-Instruct-q4f32_1-MLC` | 290 MB | a real GPU without `shader-f16` |

*Auto* (Settings → AI engine) uses the GPU only when one is actually usable — no WebGPU, a software-only adapter, a
load error or a GPU that stops mid-run all fall back to the CPU model automatically (the model chip's tooltip says
why). *CPU only* never touches the GPU. On Linux, Chrome usually ships with WebGPU switched off, so the CPU model is
used there; Macs with Apple silicon should get the GPU build in Chrome, Edge or Safari (not tested on a Mac).

Measured on an 8-thread laptop with Intel Iris Xe: CPU — model load 10–25 s, about 20 s per rewritten line, and the
page pauses a few seconds as each line starts (the model reads its instructions); GPU — model load about 7 s, about
12 s per line.

To re-download them or slim them down: `python fetch_models.py` (everything), `--webllm q4f16` (one GPU
build), `--webllm` with nothing after it (CPU model only). Re-running skips files already present. You can delete
a GPU folder you don't need; keep `models/onnx`.

## Applying

* The apply window uses the browser profile in your personal folder, so Workday logins persist between applications.
* **Workday account from `.env`** — every company runs its own Workday site, so you need an account per company.
  When the window reaches Workday's **Create Account** form, Job Agent types `WORKDAY_EMAIL` into the email field and
  `WORKDAY_PASSWORD` into both password fields; on **Sign In** it fills email and password. It never ticks the terms
  box and never clicks **Create Account** or **Sign In** — you do. It only fills empty fields, only once per form, and
  only on the posting's own Workday address (never on a single-sign-on page or any other site). A company-specific
  login can go in `.env` as `NVIDIA_WORKDAY_EMAIL=` / `NVIDIA_WORKDAY_PASSWORD=` (the first part of its Workday URL).
  Edits to `.env` apply to the next apply window; no restart needed.
* **Passwords in your OS keychain (recommended)** — Settings → *Workday account* saves a password in Windows
  Credential Manager, the macOS Keychain or the Linux Secret Service instead of `.env` (optionally for one company,
  e.g. `NVIDIA`), and *Move .env passwords to the keychain* moves what is already in `.env` and blanks it there. A
  keychain password wins over the same one in `.env`; a company's own password wins over the general one. The page
  can store or delete a password but never read one back.
* It fills: first/last name, email (your profile email, or `WORKDAY_EMAIL` if blank), phone, phone type, address,
  city, state, ZIP, country, LinkedIn/GitHub/website, *authorized to work in the US*, *need sponsorship*,
  *previously worked here*, and *how did you hear* (when it is a list). It never overwrites something you typed, never
  refills a field once you have clicked into, edited or cleared it (so you can change what it filled), and
  **never clicks Next, Submit or a consent box**.
* **My Experience:** each *Work Experience* and *Education* entry on the page gets the matching entry from
  `master_resume.yaml`, most recent first: job title, company, location, *I currently work here*, from/to dates (a
  month only when your resume gives one), the role description (your bullets), school, degree, field of study and
  years. Workday starts with no entries; tick Settings → *Autofill* → *…click Workday's Add buttons* and it adds one
  per job and school. That **Add** / **Add Another** button inside those two sections is the only button autofill
  ever clicks.
* **Voluntary disclosures** (gender, ethnicity, veteran status, disability) are set to *Decline to answer* by
  default: the window picks the "I don't wish to answer" option. Settings → *Autofill* can leave any of them to you.
  Nothing about them is stored, and the self-identification form's name and date (your signature) are left to you.
* **Answer bank:** when you answer one of a company's own questions ("Are you willing to relocate?", "Salary
  expectations", "Why do you want to work here?"), Job Agent remembers it. The next form that asks the same question,
  or one worded almost the same, gets your answer filled in and **outlined amber** so you check it (the outline's
  tooltip says where the answer came from, e.g. *written for Globex*). Questions that differ in what they ask — "in
  the US" vs "in Canada", "now" vs "in the future" — never share an answer. The page only receives answers to the
  questions it shows. Settings → *Answer bank…* lists them to edit, add or delete; your cover letters' short answers
  are added as drafts. Contact details, dates and disclosures are never stored.
* **Still to fill:** the window's banner lists the required fields on the current page that are still empty; click
  one to jump to it.
* The field patterns are in `app/autofill_rules.json`, read whenever an apply window opens: when a company labels a
  field differently, adding a pattern there is enough.
* When Workday shows a resume upload it attaches your tailored resume (DOCX by default; Settings → PDF), and your
  cover letter where an upload field asks for one.
* After you submit, the job is marked *applied* automatically when the confirmation page appears, or click
  **I submitted — mark as applied** (in the Workday window) / **Mark applied** (in Job Agent).
* Workday changes its forms often and every company configures its own questions, so autofill is best-effort.
  Always check each step.
* Postings from Greenhouse, Lever, Ashby and SmartRecruiters have **Open posting** instead of *Apply with autofill*:
  it saves the package and opens the posting in the same window, but fills nothing. Fill in the form with the files
  from the package, submit it yourself, then click **Mark applied**.

## Saved searches, schedules and alerts

* **Save…** (next to the keyword boxes) names the current keywords. Pick a saved search from the list to load its
  keywords; each one keeps its own incremental cursor, so switching between them never re-pulls everything.
* A saved search can **run on its own** (every hour … once a week) while Job Agent is open, and can **alert** you
  about new postings that score at or above a threshold. Alerts show as a 🔔 chip at the top (click to see them),
  as desktop notifications once you allow them (Manage → *Allow desktop notifications*), and in a daily page in your
  personal folder: `Alerts/new-matches-YYYY-MM-DD.html`. Runs you start yourself never alert.
* **While Job Agent is closed:** `run.py --run-searches` runs whatever is due and exits. Manage (or
  `run.py --schedule-help`) shows the one-line command that registers it with Windows Task Scheduler or cron.
  If Job Agent is open at the time, the command does nothing (Job Agent already runs them).
* **Manage** lists your saved searches (edit, run now) and the last runs with their logs, which are kept across
  restarts.
* Postings found since you last opened Job Agent are marked **new**; *N new since your last visit* above the list
  shows only those.

## Pipeline

**Pipeline** (top bar) shows every job you worked on, in columns: *Preparing* (tailored, saved, apply window
opened), *Applied*, *Recruiter screen*, *Interviewing*, *Offer*, and the outcomes *Rejected*, *Withdrawn*, *Ghosted*.

* Job Agent moves a job forward by itself as you work (tailor → save package → apply window → submitted), never
  backwards: re-packaging a job you are interviewing for keeps *Interviewing*. Drag a card to any column (or use
  the *Status* menu in its details) to record what happened.
* Each job has a **timeline** (every status change, with when and how), **notes** (who you spoke to, what's next)
  and a **follow-up** date; due follow-ups are listed at the top of the board.
* Applications with no news for 21 days are flagged *quiet* with a one-click *Move to Ghosted* (Settings → *Suggest
  "Ghosted" after*). When a search finds that a posting you track was removed from the company's site, the card
  says *posting closed*; the job itself stays where it is.
* At the top: applications per week, response rate (any answer, including a rejection), interview rate, offers, and
  the interview rate by match score, so you can see which scores are worth applying to.
* **Jobs you worked on are kept forever.** Only untouched postings expire (Settings → *Keep untouched postings for*,
  5 days by default). Older applied jobs leave the search list and live on the board. The status and timeline are
  also written into each job's `application.json`, so the Applications folder is a complete record by itself; jobs
  that an earlier version of Job Agent deleted are restored from those folders when it starts.

## companies.yaml and my_companies.yaml

* **`companies.yaml` in the program folder** is the shared list for everyone who uses this copy of Job Agent. Edit it
  any time: it is read fresh for every search and whenever Settings opens, so changes apply without a restart.
* **`my_companies.yaml` in your personal folder** holds your own additions (Settings → *Add company* writes here). An
  entry with the same URL as a shared one replaces it for you, e.g. to add `enabled: false` or aliases.
* **Settings → Companies** lists both (yours are marked *(yours)*). Untick a company to skip it; companies added to
  either file later are searched automatically.

```yaml
companies:
  # --- Technology & software
  - name: "NVIDIA"
    url: https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
  - name: "Meta"
    url: https://example.wd1.myworkdayjobs.com/External
    aliases: [Facebook, Meta Platforms]   # optional: other names, for the current-employer exclusion
  - name: "Some Company"
    url: https://tenant.wd1.myworkdayjobs.com/External
    enabled: false                        # optional: keep the entry but skip it
```

Find a URL by opening the company's careers page, following it into Workday, and copying the address up to the site
name (drop `/job/...` and any `en-US/`). Both `*.myworkdayjobs.com/<site>` and `wdN.myworkdaysite.com/recruiting/<tenant>/<site>`
addresses work. A YAML mistake or a URL that isn't a supported site shows up in red in Settings and in the search Log.

The other job boards are recognized from their own addresses:

| Board | URL |
|---|---|
| Greenhouse | `https://job-boards.greenhouse.io/<board>` (or `boards.greenhouse.io/<board>`) |
| Lever | `https://jobs.lever.co/<board>` |
| Ashby | `https://jobs.ashbyhq.com/<board>` |
| SmartRecruiters | `https://jobs.smartrecruiters.com/<company>` |

When a company's careers page lives on its own domain but is powered by one of these boards, name the board:

```yaml
  - name: "Airbnb"
    url: https://careers.airbnb.com/
    ats: greenhouse        # workday, greenhouse, lever, ashby or smartrecruiters
    board: airbnb          # the board's name (the part after the board's host in its URL)
```

A job you track on one of these boards is marked *closed* on the Pipeline board once it disappears from the board.

## Privacy

Everything stays on this computer, in your personal folder. The only network traffic is to the Workday sites and
job boards you list. Your Workday password is safest in the OS keychain (Settings → *Workday account*); if you keep it in `.env`
instead it is plain text, protected by your user account like the rest of your personal folder (on macOS/Linux Job
Agent makes the folder readable only by you). Either way, use a password you don't use anywhere else. The project
folder contains no personal data, so it can be shared or copied (leave out `.venv/`).

The apply window keeps each site's own security rules (Job Agent does not switch off Content-Security-Policy), and it
hands your profile and resume only to Workday career-site pages: a sign-on page or any other site opened in that
window gets nothing.

## Limits worth knowing

* One search query pages up to 1,000 results per company and job type; add mandatory keywords if a company hits that.
* Location, salary, remote and job-type detection are heuristics over what each company publishes. Unknowns are shown
  as *Unspecified* / *not listed* rather than guessed.
* Resume parsing works best with a simple one-column resume with standard headings (Summary, Skills, Experience,
  Education…). Check the YAML before saving.
* The 0.5B model makes mistakes the checks can't all catch (it once turned "migrated a warehouse *to* Snowflake"
  around). Read every green line.

## Project layout

```
start.bat            Windows: one-click setup + launch (passes --profile / --home / --port to run.py)
start.command        macOS: double-click in Finder (runs start.sh)
start.sh             macOS / Linux: setup + launch, same options
run.py               picks your personal folder and a free port, starts the local server
fetch_models.py      re-downloads the Qwen model files + browser libraries if they are missing
companies.yaml       the shared list of career sites (read fresh for every search)
app/                 Python backend (FastAPI)
  config.py          project vs personal paths     master.py   master_resume.yaml: draft, checks, save, sync
  workday.py         Workday API client            search.py   incremental/full search + filtering
  sources/           Greenhouse, Lever, Ashby, SmartRecruiters: URL detection, postings, US filter
  jobparse.py        salary/type/remote/state, posting sections + requirements   scoring.py  match score + reasons
  candidate.py       your years, seniority, degree from master_resume.yaml        lexicon.py  keyword dictionary
  resume_io.py       read files, write DOCX/PDF/TXT apply.py   packages + Playwright apply window + .env sign-up fill
  envfile.py         .env + OS keychain passwords  autofill.js script injected into Workday application pages
  formfill.py        what autofill.js gets: profile, work history, rules (autofill_rules.json), disclosures
  answers.py         the answer bank: remember, match and reuse answers to application questions
  pipeline.py        statuses, timeline, follow-ups, board + statistics, recovery from Applications/
  scheduler.py       saved searches on a schedule   alerts.py  new-match alerts + daily digest
  schemas.py         request bodies of the local API
static/              browser UI
  js/llm.js          Qwen engine (CPU default, GPU when available)
  js/resume-parse.js model step that builds master_resume.yaml
  js/tailor.js       live tailoring + line checks   js/resume-view.js  rendering, diff, undo controls
  js/coverletter.js  cover letter + short answers (plain versions from your bullets, AI rewrites, checks)
  js/pipeline.js     the Pipeline board
  js/searches.js     saved searches, run history, alerts
models/              Qwen2.5-0.5B files (CPU model in 6 parts)    static/vendor/  WebLLM, Transformers.js, ONNX Runtime
licenses/            third-party license texts (see THIRD_PARTY_NOTICES.md)
samples/             fictional sample resume for trying the app
tests/               pytest suite (+ tests/js for the browser checks)
tools/calibrate.py   checks the score weights against your own outcomes
requirements.txt     packages you edit       requirements.lock  exact, hash-checked versions the start scripts install
```

## Development

The tests need no model files and never touch a real Workday site or job board (searches run against recorded
JSON in `tests/fixtures/`). The autofill tests run `autofill.js` in headless Chrome, Edge or Chromium against
hand-built pages shaped like Workday's (`tests/fixtures/workday_pages/`); they are skipped when none of those
browsers is installed. With the environment the start script created:

```
.venv\Scripts\python -m pip install -r requirements-dev.txt     (macOS/Linux: .venv-<os>/bin/python ...)
.venv\Scripts\python -m pytest                                  backend tests
.venv\Scripts\python -m ruff check .                            lint
npm test                                                        tailoring checks in static/js (Node 22+)
```

After changing `requirements.txt`, regenerate `requirements.lock` with the command at the top of that file.
GitHub Actions (`.github/workflows/ci.yml`) runs all of the above on Windows, macOS and Linux.

## Troubleshooting

* **"This Job Agent belongs to another person or profile"** — open it with its start script (or the link it prints);
  bookmarks keep working after that.
* **The page pauses while tailoring** — normal on the CPU model: a few seconds at the start of each line.
* **"Model failed to load"** — use Chrome or Edge, or Settings → AI engine → CPU only.
* **macOS: "start.command can't be opened"** — right-click it → *Open* once. If double-clicking does nothing (the
  file lost its executable flag when copied), run once in Terminal: `chmod +x start.command start.sh`, or just
  `bash start.sh`.
* **Linux: "python3-venv" message** — without admin rights `start.sh` installs pip into its own environment from
  bootstrap.pypa.io and carries on; with admin rights you can `sudo apt install python3-venv` instead.
* **`./start.sh: Permission denied`** — run `bash start.sh`, or `chmod +x start.sh` once.
* **Apply window doesn't open / no PDF** — install Chrome, Edge or Chromium (Linux: `chromium` from your package
  manager works too), or let Playwright fetch its own: `<environment>/bin/python -m playwright install chromium`
  (Windows: `.venv\Scripts\python -m playwright install chromium`). The error is shown in the app.
* **A company returns errors** — check its URL in `companies.yaml` (or your `my_companies.yaml`); the search **Log**
  shows details.
