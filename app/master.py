"""
master_resume.yaml: the master resume as structured data, kept in your personal folder.

    upload -> resume_io.parse_resume() reads the lines -> draft() makes a rule-based first guess
           -> the browser's local Qwen model splits the header and every job / education heading into fields
              (bullets and summary are copied word for word; the model never retypes them)
           -> save() normalizes and writes master_resume.yaml

Tailoring always works on a copy. Only a new upload or your own edit (in the app or a text editor) changes it.
"""
import json
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache

import yaml

from . import db
from .config import MASTER_YAML
from .resume_io import (ENTRY_CORE, ENTRY_KINDS, TECH_LABEL_WORDS, entry_fields, is_extra, label_of, section_kind,
                        split_items, to_text)

KINDS = ("summary", "skills", "experience", "projects", "education", "certifications", "other")

# ---------------------------------------------------------------- patterns
_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
_YEAR = r"(?:19|20)\d{2}"
_DATE = rf"(?:{_MONTH},?\s+{_YEAR}|(?:Spring|Summer|Fall|Autumn|Winter)\s+{_YEAR}|\d{{1,2}}/{_YEAR}|{_YEAR})"
_NOW = r"(?:Present|Current|Now|Today|Ongoing)"
RANGE_RE = re.compile(rf"\(?\s*\b({_DATE})\s*(?:-|–|—|to|until|through)\s*({_DATE}|{_NOW})\b\s*\)?", re.I)
SINGLE_RE = re.compile(rf"\(?\s*\b(?:(?:Expected|Graduated|Graduation|Grad\.?)\s+)?({_DATE})\b\s*\)?", re.I)

_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
           "ND OH OK OR PA PR RI SC SD TN TX UT VT VA WA WV WI WY").split()
_STATE_NAMES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
                "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
                "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri",
                "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
                "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
                "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
                "West Virginia", "Wisconsin", "Wyoming", "District of Columbia", "Puerto Rico"]
LOC_RE = re.compile(r"\b(?:[A-Z][A-Za-z.'-]+\s){0,3}[A-Z][A-Za-z.'-]+,\s*(?:" + "|".join(_STATE_NAMES + _STATES)
                    + r")\b(?:,?\s*(?:USA|US|United States))?(?:\s+\d{5})?(?!\s+[A-Z][a-z])"
                    + r"|\b(?:Remote|Hybrid)\b(?:\s*\((?:US|USA|United States)\))?")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\d\w])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?:\s*(?:x|ext\.?)\s*\d+)?(?!\d)")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:[A-Za-z0-9-]+\.)+(?:com|io|dev|me|net|org|ai|co|app|page|site|tech|"
                    r"info|us)(?:/[^\s|,;]*)?(?![\w.])", re.I)
TITLE_RE = re.compile(r"\b(engineer|developer|manager|analyst|scientist|director|lead|architect|consultant|specialist|"
                      r"administrator|designer|intern|associate|coordinator|officer|president|head|vp|technician|"
                      r"programmer|researcher|accountant|representative|advisor|assistant|supervisor|founder|"
                      r"executive|strategist|recruiter|nurse|teacher|tester|editor|writer|principal|staff|senior|"
                      r"junior|sr|jr)\b", re.I)
ORG_RE = re.compile(r"\b(inc|llc|ltd|corp|corporation|company|group|technologies|technology|labs|systems|solutions|"
                    r"bank|university|college|institute|partners|holdings|services|consulting|software|agency|"
                    r"foundation|hospital)\b", re.I)
DEGREE_RE = re.compile(r"(?<![A-Za-z])(B\.?\s?S\.?c?|B\.?\s?A\.?|M\.?\s?S\.?c?|M\.?\s?A\.?|MBA|Ph\.?\s?D\.?|B\.?\s?Tech|"
                       r"M\.?\s?Tech|B\.?\s?E\.?|M\.?\s?E\.?|BBA|Bachelor|Master|Associate|Doctor(?:ate)?|Diploma|"
                       r"Certificate)(?![a-z])")
SCHOOL_RE = re.compile(r"\b(University|College|Institute|School|Academy|Polytechnic)\b", re.I)
_SEP = r"[|•·▪●]"
_STOP = {"at", "in", "of", "the", "and", "to", "for", "a", "an", "from", "with"}


def _s(v) -> str:
    return "" if v is None else re.sub(r"\s+", " ", str(v)).strip()


def _ml(v) -> str:
    return "\n".join(x for x in (_s(line) for line in str(v or "").splitlines()) if x)


def _list(v):
    if v is None:
        return []
    if isinstance(v, (str, int, float, dict)):
        v = [v]
    # a list item the YAML read as a mapping ("- Built X: did Y") is kept as its text
    return [x for x in (_s("; ".join(f"{k}: {t}" for k, t in i.items()) if isinstance(i, dict) else i) for i in v) if x]


def _links(v):
    """Contact links: a plain string ("linkedin.com/in/jane") or {text: LinkedIn, url: https://...} for a link
    with its own wording. A mapping without a url is just its text."""
    if isinstance(v, (str, dict)):
        v = [v]
    out = []
    for x in v or []:
        if isinstance(x, dict):
            url, text = _s(x.get("url")), _s(x.get("text"))
            x = {"text": text, "url": url} if url and text else (url or text)
        else:
            x = _s(x)
        if x:
            out.append(x)
    return out


def tidy(s: str) -> str:
    """Remove separators left dangling after a piece of a heading was taken out."""
    s = re.sub(r"\(\s*\)", " ", s)
    s = re.sub(rf"\s*{_SEP}\s*(?={_SEP}|$)", "", s)
    s = re.sub(rf"^\s*{_SEP}\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ,;|–—-")


def split_dates(text: str):
    """(start, end, text without the dates)."""
    m = RANGE_RE.search(text)
    if m:
        return m.group(1).strip(), m.group(2).strip(), tidy(f"{text[:m.start()]} | {text[m.end():]}")
    last = None
    for last in SINGLE_RE.finditer(text):
        pass
    if last:
        return "", last.group(1).strip(), tidy(f"{text[:last.start()]} | {text[last.end():]}")
    return "", "", tidy(text)


# ---------------------------------------------------------------- rule-based draft
def _parts(rest: str, kind: str):
    out = []
    for p in re.split(rf"\s*(?:{_SEP}|—|–|\s-\s)\s*", rest):
        splitter = r",\s+(?!(?:Inc|LLC|Ltd|Corp|Co)\b)" + (r"|\s+at\s+|\s+@\s+" if kind == "experience" else "")
        out += [x.strip(" ,;") for x in re.split(splitter, p) if x.strip(" ,;")]
    return out


def guess_fields(kind: str, rest: str) -> dict:
    loc = ""
    m = LOC_RE.search(rest)
    if m:
        loc = m.group(0).strip()
        rest = tidy(f"{rest[:m.start()]} | {rest[m.end():]}")
    parts = _parts(rest, kind)
    if kind == "experience":
        title = next((p for p in parts if TITLE_RE.search(p) and not ORG_RE.search(p)), "")
        others = [p for p in parts if p != title]
        company = next((p for p in others if ORG_RE.search(p)), others[0] if others else "")
        if not title:
            title = next((p for p in parts if p != company), "")
        return {"title": title, "company": company, "location": loc}
    if kind == "education":
        degree = next((p for p in parts if DEGREE_RE.search(p) and not SCHOOL_RE.search(p)), "")
        others = [p for p in parts if p != degree]
        school = next((p for p in others if SCHOOL_RE.search(p)), others[0] if others else "")
        if not degree:
            degree = next((p for p in parts if p != school), "")
        return {"degree": degree, "school": school, "location": loc}
    return {"name": rest, "location": loc}


def _skill_group(text: str):
    m = re.match(r"^([^:]{1,40}):\s*(.+)$", text)
    name, body = (m.group(1).strip(), m.group(2)) if m else ("", text)
    items = split_items(body)
    if len(items) < 2 or any(len(i.split()) > 6 for i in items):
        return None
    return {"name": name, "items": items}


def _entries(kind: str, blocks):
    entries, cur = [], None

    def new(src):
        e = {"_src": src, "_dated": False, "description": "", "bullets": [], "_before": [], "_after": []}
        entries.append(e)
        return e

    for b in blocks:
        t = b["text"]
        dated = bool(RANGE_RE.search(t) or SINGLE_RE.search(t))
        # This entry's own labelled line ("Technologies: ..."), kept as a field of it: not the next entry's heading,
        # and not a bullet even when the resume put a bullet in front of it
        label = None if dated or cur is None else label_of(t, TECH_LABEL_WORDS) if b["type"] == "bullet" else label_of(t)
        if label:
            cur["_after" if cur["bullets"] else "_before"].append(label)
            continue
        if b["type"] == "bullet":
            cur = cur or new([])
            cur["bullets"].append(t)
            continue
        sentence = len(t) > 110 or (t.endswith(".") and len(t.split()) > 8)
        if sentence and cur is not None:
            if cur["bullets"]:
                cur["bullets"].append(t)  # a bullet whose glyph was lost
            else:
                cur["description"] = f"{cur['description']} {t}".strip()
            continue
        if cur is None or cur["bullets"] or cur["description"] or (dated and cur["_dated"]):
            cur = new([t])
        else:
            cur["_src"].append(t)
        cur["_dated"] = cur["_dated"] or dated
    out = []
    for e in entries:
        src = e.pop("_src")
        e.pop("_dated")
        before, after, bullets = e.pop("_before"), e.pop("_after"), e.pop("bullets")
        start, end, rest = split_dates(" | ".join(src))
        fields = {f: "" for f in entry_fields(kind)}
        fields.update(guess_fields(kind, rest) if rest else {})
        fields.update(start=start, end=end)
        out.append({**fields, **e, **dict(before), "bullets": bullets, **dict(after), "_src": src, "_rest": rest})
    return out


def draft_contact(lines):
    text = "\n".join(lines)
    emails = EMAIL_RE.findall(text)
    t = EMAIL_RE.sub(" ", text)
    phones = [p.strip() for p in PHONE_RE.findall(t)]
    t = PHONE_RE.sub(" ", t)
    links = [u.rstrip(".") for u in URL_RE.findall(t)]
    t = URL_RE.sub(" ", t)
    rest_lines = [x for x in (tidy(line) for line in t.split("\n")) if x]
    flat = [p.strip() for line in rest_lines for p in re.split(rf"\s*{_SEP}\s*|\s{{2,}}|\s[-–—]\s", line) if p.strip()]
    name = flat[0] if flat and re.fullmatch(r"[A-Za-z][A-Za-z.'’ -]{1,60}", flat[0]) and len(flat[0].split()) <= 5 else ""
    location = next((p for p in flat if p != name and LOC_RE.fullmatch(p)), "")
    headline = next((p for p in flat[:4] if p not in (name, location) and not re.search(r"\d", p) and len(p) < 80), "")
    other = [p for p in flat if p not in (name, location, headline)]
    return {"name": name, "headline": headline,
            "contact": {"email": emails[0] if emails else "", "phone": phones[0] if phones else "",
                        "location": location, "links": links, "other": emails[1:] + phones[1:] + other},
            "_header_rest": "\n".join(rest_lines)}


def draft(structured: dict) -> dict:
    """Rule-based first guess at the YAML. Keys starting with '_' are hints for the model step."""
    c = draft_contact(structured.get("header") or [])
    out = {"name": c["name"], "headline": c["headline"], "contact": c["contact"], "sections": [],
           "_header_rest": c["_header_rest"]}
    for s in structured.get("sections") or []:
        kind, blocks = s["kind"], s["blocks"]
        # _lines: the section as written, for the model to rewrite as YAML (see loose_part)
        sec = {"title": s["title"], "kind": kind,
               "_lines": [("• " if b["type"] == "bullet" else "") + b["text"] for b in blocks]}
        if kind == "summary":
            sec["text"] = "\n".join(b["text"] for b in blocks if b["type"] != "bullet")
            sec["bullets"] = [b["text"] for b in blocks if b["type"] == "bullet"]
        elif kind == "skills":
            groups = [(_skill_group(b["text"]), b["text"]) for b in blocks]
            sec["groups"] = [g for g, _ in groups if g]
            sec["lines"] = [t for g, t in groups if not g]
        elif kind in ENTRY_KINDS:
            sec["entries"] = _entries(kind, blocks)
        else:
            bullets = sum(b["type"] == "bullet" for b in blocks)
            sec["items"] = [b["text"] for b in blocks]
            sec["style"] = "bullets" if bullets and bullets * 2 >= len(blocks) else "lines"
        out["sections"].append(sec)
    return out


# ---------------------------------------------------------------- normalize / YAML
# master_resume.yaml is read loosely, so a hand-edited file or the model's YAML of an upload never loses a line:
# other names for the usual keys are understood (position -> title, employer -> company, dates -> start/end,
# responsibilities -> bullets...), a section's kind comes from its title when it has none, any section may hold
# entries, and every other key of an entry (Technologies, GPA, Tools...) is kept as its own "Label: value" line.
def _words(s: str):
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _key(k) -> str:
    return re.sub(r"[\s_-]+", "_", str(k).strip().rstrip(":").strip().lower())


def _flat(v) -> str:
    """One line of text from a value of any shape: a list is joined, a mapping becomes "key: value; ..."."""
    if v is None:
        return ""
    if isinstance(v, dict):
        return "; ".join(f"{_s(k)}: {t}" for k, t in ((k, _flat(x)) for k, x in v.items()) if t)
    if isinstance(v, (list, tuple)):
        parts = [t for t in (_flat(x) for x in v) if t]
        return ("; " if any(len(p.split()) > 6 for p in parts) else ", ").join(parts)
    return _s(v)


def _as_list(v):
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


_COMMON_KEYS = {
    "location": ("location", "city", "place"), "start": ("start", "from", "start_date", "started"),
    "end": ("end", "to", "end_date", "until", "ended"), "dates": ("dates", "date", "duration", "period", "when"),
    "heading": ("heading",), "description": ("description", "summary", "about", "overview"),
    "bullets": ("bullets", "responsibilities", "highlights", "achievements", "accomplishments", "details", "points",
                "duties", "bullet_points"),
}
_NAME_KEYS = {  # the two heading fields of each kind; core names of other kinds map here too, so nothing is lost
    "experience": {"title": ("title", "position", "role", "job_title", "job", "name", "degree"),
                   "company": ("company", "employer", "organization", "org", "company_name", "firm", "school")},
    "education": {"degree": ("degree", "qualification", "program", "title", "name"),
                  "school": ("school", "institution", "university", "college", "company", "organization")},
    "projects": {"name": ("name", "project", "project_name", "title", "degree"),
                 "organization": ("organization", "org", "company", "school")},
}
_GENERIC_KEYS = {"name": ("name", "title", "certification", "award", "publication", "degree"),
                 "organization": ("organization", "org", "issuer", "company", "school", "institution", "publisher")}


@lru_cache(maxsize=None)
def _synonyms(kind: str) -> dict:
    out = {}
    for canon, names in {**_NAME_KEYS.get(kind, _GENERIC_KEYS), **_COMMON_KEYS}.items():
        for n in names:
            out.setdefault(n, canon)
    return out


def _label(k: str) -> str:
    """How an extra line's key is shown ("Tech. Stack:" -> "Tech Stack"); never the name of a core field."""
    label = re.sub(r"\s+", " ", str(k).replace(".", " ")).strip().rstrip(":").strip()
    return label[:1].upper() + label[1:] if label in ENTRY_CORE else label


def _entry(kind: str, e) -> dict:
    if not isinstance(e, dict):
        e = {"heading": [e]}
    fields = entry_fields(kind)
    syn = _synonyms(kind)
    got, before, after = {}, [], []
    for k, v in e.items():
        if str(k).startswith("_"):
            continue
        canon = syn.get(_key(k))
        if canon and canon not in got:
            got[canon] = v
        elif _flat(v):  # an extra line, in its place before or after the bullets
            (after if "bullets" in got else before).append((_label(k), _flat(v)))
    o = {f: _s(_flat(got.get(f))) for f in fields}
    if got.get("dates") and not (o["start"] or o["end"]):
        o["start"], o["end"], _ = split_dates(_flat(got["dates"]))
    heading = _list(got.get("heading"))
    src = _list(e.get("_src"))
    if not heading and src:
        covered = set().union(*(_words(v) for v in o.values()))
        # Keep the heading exactly as written when the fields would drop any of its words.
        if (_words(" ".join(src)) - covered - _STOP) or not (o[fields[0]] or o[fields[1]]):
            heading = src
    o["heading"] = heading
    o["description"] = _ml(_flat(got.get("description")) if not isinstance(got.get("description"), str)
                           else got.get("description"))
    for label, text in before:
        o.setdefault(label, text)
    o["bullets"] = _list(got.get("bullets"))
    for label, text in after:
        o.setdefault(label, text)
    return o


def _groups(v) -> list:
    """Skill groups from a list of {name, items}, "Name: a, b" lines, or a {name: items} mapping."""
    if isinstance(v, dict):
        v = [{"name": k, "items": x} for k, x in v.items()]
    out = []
    for g in _as_list(v):
        if isinstance(g, dict) and not ({"name", "items"} & set(g)):
            out += _groups({k: x for k, x in g.items()})
            continue
        if isinstance(g, dict):
            name, items = _s(g.get("name")), g.get("items")
        else:
            m = re.match(r"^([^:]{1,40}):\s*(.+)$", _s(g))
            name, items = (m.group(1).strip(), m.group(2)) if m else ("", _s(g))
        items = [i for x in _as_list(items) for i in (split_items(x) if isinstance(x, str) else [_flat(x)])
                 if i] if isinstance(items, (str, list, tuple)) else _list(items)
        if items:
            out.append({"name": name, "items": items})
    return out


def _section(sec: dict) -> dict:
    keys = {_key(k): v for k, v in sec.items() if not str(k).startswith("_")}
    title = _s(keys.get("title"))
    kind = keys.get("kind") if keys.get("kind") in KINDS else section_kind(title)
    o = {"title": title or kind.title(), "kind": kind}

    def pick(*names):
        return next((keys[n] for n in names if keys.get(n) not in (None, "", [], {})), None)

    if kind == "summary":
        text = pick("text", "summary", "paragraph", "content", "description", "about", "profile")
        o["text"] = _ml("\n".join(_flat(t) for t in _as_list(text)))
        o["bullets"] = _list(pick("bullets", "highlights", "points", "items"))
    elif kind == "skills":
        o["groups"] = _groups(pick("groups", "categories", "entries"))
        o["lines"] = _list(pick("lines", "text"))
        flat = pick("items", "skills", "list")
        if flat:
            o["groups"] += _groups([{"name": "", "items": flat}])
    elif kind in ENTRY_KINDS:
        raw = pick("entries", "jobs", "positions", "roles", "schools", "degrees", "projects", "items")
        o["entries"] = [_entry(kind, e) for e in _as_list(raw)]
    else:
        entries = [_entry(kind, e) for e in _as_list(pick("entries")) if isinstance(e, dict)]
        lines = []
        for it in _as_list(pick("items", "list", "lines", "text", "certifications", "awards")):
            if isinstance(it, dict):
                entries.append(_entry(kind, it))
            else:
                lines.append(it)
        if entries:
            o["entries"] = entries
        o["items"] = _list(lines)
        o["style"] = "bullets" if keys.get("style") == "bullets" else "lines"
    return o


def _sections_list(sections) -> list:
    """sections as a list; a mapping {title: content} (a loosely written file) is turned into one."""
    if not isinstance(sections, dict):
        return sections
    out = []
    for title, body in sections.items():
        if isinstance(body, dict):
            out.append({"title": title, **body})
        elif isinstance(body, list):
            out.append({"title": title, ("entries" if body and all(isinstance(x, dict) for x in body) else "items"): body})
        else:
            out.append({"title": title, "text": body})
    return out


def normalize(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("master_resume.yaml must be a mapping with name, contact and sections")
    top = {_key(k): v for k, v in data.items()}
    c = {_key(k): v for k, v in (top.get("contact") if isinstance(top.get("contact"), dict) else {}).items()}

    def get(name):  # a contact field, inside contact: or (loosely) at the top
        return c.get(name) if c.get(name) not in (None, "") else top.get(name)

    links = _links(c.get("links") or top.get("links"))
    for name in ("linkedin", "github", "website", "portfolio"):
        if _s(get(name)) and _s(get(name)) not in links:
            links.append(_s(get(name)))
    out = {"name": _s(top.get("name")), "headline": _s(top.get("headline") or top.get("tagline")),
           "contact": {"email": _s(get("email")), "phone": _s(get("phone")), "location": _s(get("location")),
                       "links": links, "other": _list(c.get("other"))},
           "sections": []}
    sections = _sections_list(top.get("sections") or [])
    if not isinstance(sections, list):
        raise ValueError("'sections' must be a list")
    for sec in sections:
        if isinstance(sec, dict):
            out["sections"].append(_section(sec))
    if not out["name"] and not out["sections"]:
        raise ValueError("No name and no sections found")
    return out


def _compact(v):
    """Leave empty fields out of the YAML so it stays readable."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            x = _compact(x)
            if x in ("", [], {}, None) and k not in ("title", "kind"):
                continue
            if k == "style" and x == "lines":
                continue
            out[k] = x
        return out
    if isinstance(v, list):
        return [_compact(x) for x in v if x not in ("", None)]
    return v


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):  # indent list items under their key
        return super().increase_indent(flow, False)


HEADER = """\
# master_resume.yaml - your master resume as data. Job Agent tailors a COPY of it for each job;
# only a new upload or your own edit changes this file (edit here, or in Job Agent: Master tab > Edit YAML).
#
# sections are printed in this order; kind is one of: summary, skills, experience, projects, education,
#   certifications, other.
# contact links: a plain address (linkedin.com/in/you shows as "LinkedIn", clickable) or
#   - text: Portfolio
#     url: https://example.com
# experience entries: title, company, location, start, end, description, bullets
# education entries:  degree, school, location, start, end, description, bullets
# Any other line of an entry (Technologies: ..., GPA: ...) is kept and printed as "Label: value".
# heading: when an entry has one, those lines are printed exactly as written instead of the fields above.
"""


TAILORED_HEADER = """\
# tailored_resume.yaml - the tailored copy of your master resume used for this one application.
# Your master_resume.yaml is unchanged. Same layout as master_resume.yaml.
"""


def dump(data: dict, note: str = "", header: str = HEADER) -> str:
    body = yaml.dump(_compact(normalize(data)), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=4096,
                     default_flow_style=False)
    return header + (f"# {note}\n" if note else "") + "\n" + body


def parse_yaml(text: str) -> dict:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" on line {mark.line + 1}" if mark else ""
        raise ValueError(f"YAML error{where}: {getattr(e, 'problem', None) or e}") from None
    return normalize(raw)


# ---------------------------------------------------------------- the model's YAML for one part of an upload
# The model rewrites each section (or, for a long one, each job) of an uploaded resume as loose YAML. Its YAML is
# used only when it is that part of the resume, rearranged into fields and nothing else: every value is found in it
# (a typo is put back to your own wording), no line is used twice or given to the wrong job, entries keep their
# order, every line is used, and every label is one the resume uses. Otherwise the rule-based parse is kept.
_FENCE = re.compile(r"^\s*```[A-Za-z]*[ \t]*\n?|\n?[ \t]*```\s*$")
_TOKEN = re.compile(r"[A-Za-z0-9+#%$&]+")
_PLAIN = re.compile(r"^(\s*(?:-\s+)?)([A-Za-z][\w .&/()'-]{0,40}):[ \t]+(.+?)\s*$")
_ITEM = re.compile(r"^(\s*-\s+)(.+?)\s*$")
# Words of a line that may be left out of the fields ("Engineer at Acme", "Expected May 2027").
_FILLER = {"and", "the", "of", "in", "for", "to", "a", "an", "with", "at", "on", "by", "or", "as", "from", "expected",
           "graduated", "graduation", "grad", "since", "until"}
_PLACEHOLDER = re.compile(r"^\s*(none|n/?a|null|unknown|tbd|-+|not (specified|listed|given|available|applicable|"
                          r"mentioned|provided)|no (description|details|information|data|bullets?|location|dates?)"
                          r"( (provided|available|given|listed))?)\s*\.?\s*$", re.I)
_KEY_THEN_TEXT = re.compile(r"^([A-Za-z][\w ]{0,30}):[ \t]*\n(.+)$", re.S)
_DATE_ONLY = re.compile(rf"\(?\s*(?:(?:Expected|Graduated|Graduation|Grad\.?)\s+)?(?:{_DATE}|{_NOW})\s*\)?", re.I)
# Keys that hold a part's content; any other single key around it ("entry:", "education:") is only a wrapper.
_CONTENT_KEYS = {"text", "summary", "bullets", "highlights", "points", "groups", "categories", "items", "list", "lines",
                 "skills", "entries", "jobs", "positions", "roles", "schools", "degrees", "projects", "certifications",
                 "awards"}
_HEADING_KEYS = ("title", "company", "degree", "school", "name", "organization", "location", "start", "end")


def _quote_values(text: str) -> str:
    """Second try at the model's YAML: quote plain values (a bullet with ": " in it breaks YAML)."""
    out = []
    for line in text.split("\n"):
        m = _PLAIN.match(line)
        if m and m.group(3)[0] not in "\"'[{|>":
            line = f"{m.group(1)}{m.group(2)}: {json.dumps(m.group(3), ensure_ascii=False)}"
        elif (m := _ITEM.match(line)) and m.group(2)[0] not in "\"'[{" and not _PLAIN.match(m.group(2)):
            line = f"{m.group(1)}{json.dumps(m.group(2), ensure_ascii=False)}"
        out.append(line)
    return "\n".join(out)


def _model_yaml(text: str):
    t = _FENCE.sub("", (text or "").strip()).strip()
    if not t:
        raise ValueError("the model wrote nothing")
    for attempt in (t, _quote_values(t)):
        try:
            return yaml.safe_load(attempt)
        except yaml.YAMLError:
            continue
    m = _KEY_THEN_TEXT.match(t)  # "Summary:" on its own line, then the paragraph
    if m and not re.search(r"^\s*-?\s*[\w ]{1,30}:(\s|$)", m.group(2), re.M):
        return {m.group(1): m.group(2).strip()}
    raise ValueError("the model's YAML could not be read")


def _no_placeholders(v):
    """The model's "none" / "Not Specified" for a missing field means the field is empty."""
    if isinstance(v, dict):
        return {k: _no_placeholders(x) for k, x in v.items() if not (isinstance(x, str) and _PLACEHOLDER.match(x))}
    if isinstance(v, list):
        return [_no_placeholders(x) for x in v if not (isinstance(x, str) and _PLACEHOLDER.match(x))]
    return v


def _unwrap(data, keep=()):
    """{"entry": {...}} or {"education": [...]} -> what it wraps; a content key ("items: [...]") is kept."""
    while isinstance(data, dict) and len(data) == 1:
        (k, v), = data.items()
        if _key(k) in _CONTENT_KEYS or _key(k) in keep or not isinstance(v, (dict, list)):
            break
        data = v
    return data


class _Source:
    """The text of one part of the uploaded resume, to find the model's values in."""

    def __init__(self, lines):
        self.lines = [x for x in (re.sub(r"^\s*[•●▪◦‣∙·■]\s*", "", str(line or "")).strip() for line in lines) if x]
        self.text = "\n".join(self.lines)
        self.norm = " " + " ".join(t.lower() for t in _TOKEN.findall(self.text)) + " "

    def is_label(self, words: str) -> bool:
        """Does the resume use `words` as a label ("Technologies:", "Languages -")?"""
        toks = _TOKEN.findall(words)
        return bool(toks) and re.search(r"(?<![A-Za-z0-9+#%$&])" + r"[^A-Za-z0-9+#%$&]{1,6}".join(map(re.escape, toks))
                                        + r"\s*[:–—-]", self.text, re.I) is not None

    def find(self, value: str):
        """Your own wording of `value`: the same words in the resume (any case or punctuation between them), or the
        closest stretch of one line when the model mistyped a little. None when it isn't in the resume."""
        toks = _TOKEN.findall(value)
        if not toks:
            return None
        rx = (r"(?<![A-Za-z0-9+#%$&])" + r"[^A-Za-z0-9+#%$&]{1,6}".join(map(re.escape, toks))
              + r"(?![A-Za-z0-9+#%$&])")
        m = re.search(rx, self.text, re.I)
        if m:
            span = self.text[m.start():m.end()] + self._tail(self.text, m.end(), value)
            return re.sub(r"\s*\n\s*", " ", span)
        return self._closest(toks, value)

    @staticmethod
    def _tail(text, end, value) -> str:
        """The closing punctuation after a match ("35%." / "(EMR)"), when it ends the line or the value had it."""
        tail = re.match(r"[.%)!?\]]+", text[end:])
        if not tail:
            return ""
        after = end + len(tail.group(0))
        return tail.group(0) if after == len(text) or text[after] == "\n" or value.rstrip()[-1:] in tail.group(0) else ""

    def _closest(self, toks, value):
        want = " ".join(t.lower() for t in toks)
        n = len(toks)
        best, best_ratio = None, 0.0
        for line in self.lines:
            words = list(_TOKEN.finditer(line))
            low = [w.group(0).lower() for w in words]
            for size in sorted({max(1, n - 1), n, n + 1}):
                for i in range(max(1, len(words) - size + 1)):
                    cand = " ".join(low[i:i + size])
                    sm = SequenceMatcher(None, want, cand)
                    if sm.real_quick_ratio() < 0.88 or sm.quick_ratio() < 0.88:
                        continue
                    r = sm.ratio()
                    if r > best_ratio:
                        end = words[min(i + size, len(words)) - 1].end()
                        best_ratio, best = r, line[words[i].start():end] + self._tail(line, end, value)
        return best if best_ratio >= 0.88 else None


def _snap(part, src: _Source, section_level: bool):
    """Put every value of `part` back into the resume's own wording. Returns the values that aren't in it."""
    bad = []

    def fix(v):
        if not isinstance(v, str) or not v.strip():
            return v
        lines = [x for x in v.split("\n") if x.strip()]
        got = [src.find(x) for x in lines]
        bad.extend(x for x, g in zip(lines, got) if g is None)
        return "\n".join(g or x for x, g in zip(lines, got))

    def entry(e):
        for k, v in list(e.items()):
            if isinstance(v, list):
                e[k] = [fix(x) for x in v]
            elif is_extra(k, v):
                if not src.is_label(k):
                    bad.append(f"{k}:")
                e[k] = fix(v)
            elif isinstance(v, str):
                e[k] = fix(v)

    if not section_level:
        entry(part)
        return bad
    for k in ("text", "bullets", "items", "lines"):
        if isinstance(part.get(k), list):
            part[k] = [fix(x) for x in part[k]]
        elif isinstance(part.get(k), str):
            part[k] = fix(part[k])
    for g in part.get("groups") or []:
        if g["name"] and not src.is_label(g["name"]):
            g["name"] = ""  # a group name the resume doesn't use as a label is left out, never made up
        g["items"] = [fix(x) for x in g["items"]]
    for e in part.get("entries") or []:
        entry(e)
    return bad


def _tokens_of(text: str):
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _pieces(part, scope):
    """What a part prints, in blocks: (True, [an entry's pieces]) or (False, [one piece]), in order."""
    def entry(e):
        out = list(e["heading"]) if e.get("heading") else [e[f] for f in _HEADING_KEYS if e.get(f)]
        out += (e.get("description") or "").split("\n")
        for k, v in e.items():
            if k == "bullets":
                out += v
            elif is_extra(k, v):
                out.append(f"{k}: {v}")
        return [x for x in out if x]

    if scope == "entry":
        return [(True, entry(part))]
    flat = (part.get("text") or "").split("\n") + list(part.get("bullets") or [])
    flat += [(f"{g['name']}: " if g["name"] else "") + ", ".join(g["items"]) for g in part.get("groups") or []]
    flat += list(part.get("lines") or []) + list(part.get("items") or [])
    return [(False, [x]) for x in flat if x] + [(True, entry(e)) for e in part.get("entries") or []]


class _Budget:
    """The words of each source line, used up as the model's pieces are matched to lines."""

    def __init__(self, src: _Source):
        self.lines = src.lines
        self.left = [Counter(_tokens_of(line)) for line in src.lines]

    def _fits(self, need, span):
        have = sum((self.left[j] for j in span), Counter())
        return all(have[t] >= c for t, c in need.items())

    def take(self, words, start):
        """The first line at or after `start` that still holds all of `words` (or else two lines in a row, for a
        piece the resume wrapped); they are used up. (first line, last line) or None when no line does."""
        need = Counter(words)
        for width in (1, 2):
            for i in range(start, len(self.left) - width + 1):
                span = range(i, i + width)
                if self._fits(need, span):
                    for t, c in need.items():
                        for j in span:
                            used = min(c, self.left[j][t])
                            self.left[j][t] -= used
                            c -= used
                    return i, i + width - 1
        return None

    def anywhere(self, words) -> bool:
        need = Counter(words)
        return any(self._fits(need, (i,)) for i in range(len(self.left)))


def _fields_make_sense(part, scope):
    """A location that names a school, company or job title, or a date that isn't one, means the model put a value in
    the wrong field."""
    entries = [part] if scope == "entry" else part.get("entries") or []
    for e in entries:
        loc = e.get("location") or ""
        if loc and (SCHOOL_RE.search(loc) or ORG_RE.search(loc) or TITLE_RE.search(loc) or len(loc.split()) > 6):
            raise ValueError(f"put “{loc[:40]}” in the location")
        for f in ("start", "end"):
            if e.get(f) and not _DATE_ONLY.fullmatch(e[f].strip()):
                raise ValueError(f"put “{e[f][:40]}” in a date")


def _accounted(part, scope, src: _Source):
    """Raise ValueError unless the part is the source rearranged: each piece uses up words of one line (so nothing is
    used twice), an entry never takes a line from an entry before it, the other pieces keep their order, and every
    word of every line is used (but a few filler words)."""
    budget = _Budget(src)
    floor = cursor = 0
    for is_entry, pieces in _pieces(part, scope):
        lines = []
        for piece in pieces:
            words = _tokens_of(piece)
            if not words:
                continue
            got = budget.take(words, floor if is_entry else cursor)
            if got is None:
                moved = budget.anywhere(words)
                raise ValueError(f"{'puts a line in the wrong place' if moved else 'repeats part of it'} "
                                 f"(“{piece[:40]}”)")
            lines.append(got[1])
            if not is_entry:
                cursor = got[0]
        if is_entry and lines:
            floor = max(lines)
    for line, left in zip(budget.lines, budget.left):
        if any(c > 0 and t not in _FILLER for t, c in left.items()):
            raise ValueError(f"left out part of it (“{line[:40]}…”)")


def loose_part(kind: str, title: str, scope: str, source, text: str, src=()) -> dict:
    """The section (scope "section") or entry (scope "entry", `src` = its heading lines) the model wrote as YAML in
    `text`, normalized and in your own wording. ValueError (the reason) when it can't be used."""
    kind = kind if kind in KINDS else "other"
    data = _no_placeholders(_unwrap(_model_yaml(text), _synonyms(kind) if scope == "entry" else ()))
    if scope == "entry":
        if isinstance(data, dict) and isinstance(data.get("entries"), list) and len(data["entries"]) == 1:
            data = data["entries"][0]
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError("the model's YAML is not one entry")
        part = _entry(kind, {**data, "_src": list(src or [])})
    else:
        if isinstance(data, list):
            data = {"entries" if data and all(isinstance(x, dict) for x in data) else "items": data}
        elif isinstance(data, str):
            data = {"text": data}
        elif not isinstance(data, dict):
            raise ValueError("the model's YAML is not a section")
        keys = {_key(k) for k in data}
        if kind in ENTRY_KINDS and not keys & {"entries", "jobs", "positions", "roles", "schools", "degrees",
                                               "projects", "items"}:
            data = {"entries": [data]}  # one job written without "entries:"
        data = {k: v for k, v in data.items() if _key(k) not in ("title", "kind")}
        part = _section({**data, "title": title, "kind": kind})
    where = _Source(source)
    bad = _snap(part, where, scope != "entry")
    if bad:
        raise ValueError(f"adds text that is not in your resume (“{bad[0][:50]}”)")
    _fields_make_sense(part, scope)
    _accounted(part, scope, where)
    return part


# ---------------------------------------------------------------- file <-> database
def save(data=None, yaml_text=None, filename=None, new_upload=False, note=""):
    """Write master_resume.yaml (your exact text when you edited the YAML) and refresh the DB copy."""
    if yaml_text is not None:
        data = parse_yaml(yaml_text)
    else:
        data = normalize(data)
        yaml_text = dump(data, note)
    MASTER_YAML.write_text(yaml_text, encoding="utf-8")
    current = db.get_resume()
    filename = filename or (current["filename"] if current else MASTER_YAML.name)
    db.save_resume(filename, to_text(data), data, MASTER_YAML.stat().st_mtime, clear_tailored=new_upload)
    return data, yaml_text


def sync():
    """Pick up edits made to master_resume.yaml outside the app. Returns (changed, error)."""
    current = db.get_resume()
    if not MASTER_YAML.exists():
        if current:
            db.clear_resume()
            return True, None
        return False, None
    mtime = MASTER_YAML.stat().st_mtime
    if current and current["yaml_mtime"] == mtime:
        return False, None
    try:
        data = parse_yaml(MASTER_YAML.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        return False, f"master_resume.yaml: {e} (using the last good copy)"
    db.save_resume(current["filename"] if current else MASTER_YAML.name, to_text(data), data, mtime)
    return True, None


def read_yaml() -> str:
    return MASTER_YAML.read_text(encoding="utf-8-sig") if MASTER_YAML.exists() else ""


def note_for(filename: str, how: str) -> str:
    return f"Built {datetime.now():%Y-%m-%d %H:%M} from {filename} ({how})."
