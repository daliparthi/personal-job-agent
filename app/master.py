"""
master_resume.yaml: the master resume as structured data, kept in your personal folder.

    upload -> resume_io.parse_resume() reads the lines -> draft() makes a rule-based first guess
           -> the browser's local Qwen model splits the header and every job / education heading into fields
              (bullets and summary are copied word for word; the model never retypes them)
           -> save() normalizes and writes master_resume.yaml

Tailoring always works on a copy. Only a new upload or your own edit (in the app or a text editor) changes it.
"""
import re
from datetime import datetime

import yaml

from . import db
from .config import MASTER_YAML
from .resume_io import ENTRY_FIELDS, ENTRY_KINDS, split_items, to_text

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
    if isinstance(v, (str, int, float)):
        v = [v]
    return [x for x in (_s(i) for i in v) if x]


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
        e = {"_src": src, "_dated": False, "description": "", "bullets": []}
        entries.append(e)
        return e

    for b in blocks:
        t = b["text"]
        if b["type"] == "bullet":
            cur = cur or new([])
            cur["bullets"].append(t)
            continue
        dated = bool(RANGE_RE.search(t) or SINGLE_RE.search(t))
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
        start, end, rest = split_dates(" | ".join(src))
        fields = {f: "" for f in ENTRY_FIELDS[kind]}
        fields.update(guess_fields(kind, rest) if rest else {})
        fields.update(start=start, end=end)
        out.append({**fields, **e, "_src": src, "_rest": rest})
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
        sec = {"title": s["title"], "kind": kind}
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
def _words(s: str):
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _entry(kind: str, e) -> dict:
    if not isinstance(e, dict):
        e = {"heading": [e]}
    fields = ENTRY_FIELDS[kind]
    o = {f: _s(e.get(f)) for f in fields}
    if e.get("dates") and not (o["start"] or o["end"]):
        o["start"], o["end"], _ = split_dates(_s(e["dates"]))
    heading = _list(e.get("heading"))
    src = _list(e.get("_src"))
    if not heading and src:
        covered = set().union(*(_words(v) for v in o.values()))
        # Keep the heading exactly as written when the fields would drop any of its words.
        if (_words(" ".join(src)) - covered - _STOP) or not (o[fields[0]] or o[fields[1]]):
            heading = src
    o["heading"] = heading
    o["description"] = _ml(e.get("description"))
    o["bullets"] = _list(e.get("bullets"))
    return o


def normalize(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("master_resume.yaml must be a mapping with name, contact and sections")
    c = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    out = {"name": _s(data.get("name")), "headline": _s(data.get("headline")),
           "contact": {"email": _s(c.get("email")), "phone": _s(c.get("phone")), "location": _s(c.get("location")),
                       "links": _links(c.get("links")), "other": _list(c.get("other"))},
           "sections": []}
    sections = data.get("sections") or []
    if not isinstance(sections, list):
        raise ValueError("'sections' must be a list")
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        kind = sec.get("kind") if sec.get("kind") in KINDS else "other"
        o = {"title": _s(sec.get("title")) or kind.title(), "kind": kind}
        if kind == "summary":
            o["text"] = _ml(sec.get("text"))
            o["bullets"] = _list(sec.get("bullets"))
        elif kind == "skills":
            o["groups"] = []
            for g in sec.get("groups") or []:
                if isinstance(g, dict):
                    items = g.get("items")
                    items = split_items(items) if isinstance(items, str) else _list(items)
                    if items:
                        o["groups"].append({"name": _s(g.get("name")), "items": items})
                elif _s(g):
                    o["groups"].append({"name": "", "items": split_items(_s(g))})
            o["lines"] = _list(sec.get("lines"))
        elif kind in ENTRY_KINDS:
            o["entries"] = [_entry(kind, e) for e in sec.get("entries") or []]
        else:
            o["items"] = _list(sec.get("items"))
            o["style"] = "bullets" if sec.get("style") == "bullets" else "lines"
        out["sections"].append(o)
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
