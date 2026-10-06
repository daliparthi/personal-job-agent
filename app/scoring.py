"""
ATS-style match score between a resume and a job description, with the reasons behind it.

base  = 60% weighted keyword coverage (what ATS filters look for)
      + 25% overall wording similarity (cosine over content words)
      + 15% job-title alignment (are the title's words in your resume?)

Keywords are weighed by where the posting puts them: in its required qualifications ×1.5, in the duties or overview
×1, under "preferred / nice to have" ×0.75, and not at all when they only appear in boilerplate (about the company,
benefits, pay, EEO...). With your profile (app/candidate.py) the base is then adjusted, each time with a reason:
the years of experience the posting asks for, how senior the role is compared with your latest title, and hard
requirements you don't meet (visa sponsorship, citizenship, clearance, degree). Every requirement line of the posting
is also matched to the resume line that best shows it.
"""
import math
import re
from collections import Counter
from functools import lru_cache

from .jobparse import (DEGREE_NAMES, SENIORITY_NAMES, WEIGHTLESS, degree_required, jd_sections, knockouts_in,
                       section_text, seniority_level, years_required)
from .lexicon import ACRONYM_STOP, CASE_SENSITIVE, ENTRIES

VERSION = 3  # bump when scores change meaning: stored scores are recomputed at the next start

STOPWORDS = set("""
a about above across after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her
here hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or
other our ours out over own same she should so some such than that the their them then there these they this those
through to too under until up very was we were what when where which while who whom why will with would you your
yours within across including include includes etc using use used work working works role team teams company job
position candidate candidates ability able experience experiences years year strong excellent good great new well
must preferred required requirement requirements responsibilities responsibility qualifications qualification
skills skill knowledge understanding opportunity opportunities environment ensure across based help support
supporting provide providing make making plus one two three four five per may might like least related relevant
""".split())

SENIORITY = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate", "ii", "iii", "iv", "i",
             "manager", "director", "head", "chief", "vp", "intern", "entry", "level", "mid"}

# How much a keyword counts, by the part of the posting it appears in (boilerplate: not at all).
SECTION_WEIGHT = {"required": 1.5, "intro": 1.0, "responsibilities": 1.0, "other": 1.0, "preferred": 0.75,
                  "boilerplate": 0.0}
WHERE_LABEL = {"required": "required", "preferred": "nice to have", "responsibilities": "in the duties",
               "intro": "in the overview", "other": "mentioned"}
KNOCKOUT_LABEL = {"sponsorship": "no visa sponsorship", "citizenship": "US citizens only",
                  "clearance": "security clearance", "degree": "degree"}


def _is_acronym(alias: str) -> bool:
    return " " not in alias and len(alias) <= 6 and sum(ch.isupper() for ch in alias) >= 2


def _case_sensitive(alias: str) -> bool:
    return alias in CASE_SENSITIVE or _is_acronym(alias)


@lru_cache(maxsize=None)
def _alias_regex(alias: str):
    flags = 0 if _case_sensitive(alias) else re.I
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9+#])", flags)


@lru_cache(maxsize=None)
def _needle(alias: str):
    """(text to look for with a plain substring search, whether to look in the lowercased text)."""
    return (alias, False) if _case_sensitive(alias) else (alias.lower(), True)


@lru_cache(maxsize=256)
def _lower(text: str) -> str:
    return text.lower()


def _maybe(alias, text) -> bool:
    """Cheap pre-check: most of the ~1,200 aliases are absent from a posting, and a substring search rules them out
    far faster than the word-boundary regex (which then decides)."""
    needle, low = _needle(alias)
    return needle in (_lower(text) if low else text)


def _count(aliases, text) -> int:
    return sum(len(_alias_regex(a).findall(text)) for a in aliases if _maybe(a, text))


def has_any(aliases, text) -> bool:
    return any(_alias_regex(a).search(text) for a in aliases if _maybe(a, text))


_has = has_any


def _context(aliases, text, width=170):
    for a in aliases:
        m = _alias_regex(a).search(text)
        if m:
            start = max(text.rfind("\n", 0, m.start()), text.rfind(". ", 0, m.start()), m.start() - width // 2)
            snippet = text[start + 1:m.end() + width // 2].strip()
            return ("…" if start > 0 else "") + snippet.split("\n")[0] + "…"
    return ""


def _acronyms(text):
    counts = Counter(re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9&]{1,5})(?![A-Za-z0-9])", text))
    return {a: c for a, c in counts.items() if c >= 2 and a not in ACRONYM_STOP and not a.isdigit()}


def _by_kind(sections) -> dict:
    parts = {}
    for kind, text in sections:
        parts[kind] = f"{parts.get(kind, '')}\n{text}"
    return parts


def _placement(counts: dict):
    """(occurrences outside boilerplate, the most important part of the posting it appears in) or (0, None)."""
    kinds = [k for k, c in counts.items() if c and SECTION_WEIGHT.get(k, 1.0) > 0]
    if not kinds:
        return 0, None
    return sum(counts[k] for k in kinds), max(kinds, key=lambda k: SECTION_WEIGHT.get(k, 1.0))


def extract_keywords(jd_text: str, title: str = "", extra=(), company: str = "", sections=None):
    """{canonical: {"aliases", "count", "weight", "where"}} for every ATS keyword the posting asks for."""
    sections = jd_sections(jd_text) if sections is None else sections
    parts = _by_kind(sections)
    relevant = section_text(sections)
    found = {}
    known_aliases = set()
    company_words = set(re.findall(r"[a-z0-9]+", (company or "").lower()))

    def add(canon, aliases, base):
        n, where = _placement({k: _count(aliases, t) for k, t in parts.items()})
        if n:  # only in boilerplate ("we offer a 401(k) and AWS credits") means it isn't asked for
            w = base * min(3, n) * SECTION_WEIGHT.get(where, 1.0) + (1.5 if has_any(aliases, title) else 0)
            found[canon] = {"aliases": aliases, "count": n, "weight": round(w, 2), "where": where}

    for canon, aliases, base in ENTRIES:
        known_aliases.update(a.lower() for a in aliases)
        if any(a.lower() in company_words for a in aliases):
            continue  # the employer's own name is not a skill
        if has_any(aliases, jd_text):
            add(canon, aliases, base)
    for acr in _acronyms(relevant):
        if acr.lower() not in known_aliases and acr.lower() not in company_words and acr not in found:
            add(acr, [acr], 0.8)
    for kw in extra:
        if not kw or kw.lower() in known_aliases or any(kw.lower() == c.lower() for c in found):
            # Already covered by the dictionary: just make sure it counts as important.
            for k in found.values():
                if kw and kw.lower() in (a.lower() for a in k["aliases"]):
                    k["weight"] = max(k["weight"], 2.0)
            continue
        if has_any([kw], relevant):
            n, where = _placement({k: _count([kw], t) for k, t in parts.items()})
            found[kw] = {"aliases": [kw], "count": n, "weight": 2.0, "where": where}
    return found


def _tokens(text):
    return [t for t in re.findall(r"[a-z][a-z0-9+#.]*[a-z0-9+#]|[a-z]", text.lower()) if t not in STOPWORDS]


def _vector(text):
    toks = _tokens(text)
    grams = toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
    return {t: 1 + math.log(c) for t, c in Counter(grams).items()}


def cosine(a: str, b: str) -> float:
    va, vb = _vector(a), _vector(b)
    dot = sum(v * vb.get(k, 0) for k, v in va.items())
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def title_alignment(resume_text: str, title: str) -> float:
    words = [w for w in re.findall(r"[a-z][a-z0-9+#]*", (title or "").lower())
             if w not in STOPWORDS and w not in SENIORITY and len(w) > 1]
    if not words:
        return 0.0
    low = resume_text.lower()
    return sum(1 for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}", low)) / len(words)


# ---------------------------------------------------------------- requirements of the posting
def _skill_in(text: str):
    """The first hard skill named in a stretch of text ("...of experience with Python and SQL" -> Python)."""
    best = None
    for canon, aliases, base in ENTRIES:
        if base < 1.0:
            continue  # soft skills ("communication") are not what years are counted in
        for a in aliases:
            m = _alias_regex(a).search(text) if _maybe(a, text) else None
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), canon)
    return best[1] if best else None


def requirements(sections, jd_text: str) -> dict:
    years = [{"years": y["years"], "skill": _skill_in(y["after"][:80]), "kind": y["kind"], "text": y["text"]}
             for y in years_required(sections)]
    return {"years": years, "degree": degree_required(sections), "knockouts": knockouts_in(jd_text)}


def _experience(req, prof, adjustments):
    asked = [y for y in req["years"] if y["kind"] != "preferred"]
    need = max((y["years"] for y in asked), default=None)
    have = prof.get("years_total")
    out = {"need": need, "have": have, "skills": []}
    if need and have is not None and need - have > 0.5:
        points = -min(15, round(3 * (need - have)))
        adjustments.append({"kind": "experience", "points": points,
                            "reason": f"Asks for {need}+ years of experience; your resume shows about {have:g}."})
    skill_points = 0
    seen = set()
    for y in asked:
        skill = y["skill"]
        if not skill or skill in seen:
            continue
        seen.add(skill)
        got = prof.get("years_by_skill", {}).get(skill)
        out["skills"].append({"skill": skill, "need": y["years"], "have": got})
        if got is not None and y["years"] - got > 0.5 and skill_points > -6:
            skill_points -= 2
            adjustments.append({"kind": "skill_years", "points": -2,
                                "reason": f"Asks for {y['years']}+ years of {skill}; your jobs that mention it "
                                          f"add up to about {got:g}."})
    return out


def _seniority(title, prof, adjustments):
    jl, cl = seniority_level(title), prof.get("level")
    if jl is None or cl is None or not prof.get("recent_title"):
        return None
    out = {"job": jl, "you": cl, "job_label": SENIORITY_NAMES[jl], "your_label": SENIORITY_NAMES[cl],
           "recent_title": prof["recent_title"]}
    if jl - cl >= 2:
        adjustments.append({"kind": "seniority", "points": -8,
                            "reason": f"This is {SENIORITY_NAMES[jl]}; your latest title "
                                      f"({prof['recent_title']}) reads as {SENIORITY_NAMES[cl]}."})
    elif cl - jl >= 2:
        adjustments.append({"kind": "seniority", "points": -3,
                            "reason": f"This is {SENIORITY_NAMES[jl]}, more junior than your latest title "
                                      f"({prof['recent_title']})."})
    return out


def _knockouts(req, prof, adjustments):
    out = []
    for k in req["knockouts"]:
        if k["kind"] == "sponsorship":
            blocking = prof.get("needs_sponsorship", False)
        elif k["kind"] == "citizenship":
            blocking = prof.get("us_citizen") == "No" or (not prof.get("us_citizen") and prof.get("needs_sponsorship"))
        else:  # clearance
            blocking = prof.get("has_clearance") == "No"
        out.append({**k, "label": KNOCKOUT_LABEL[k["kind"]], "blocking": bool(blocking)})
    degree = req["degree"]
    if degree and prof.get("has_education") and (prof.get("degree") or 0) < degree["level"]:
        out.append({"kind": "degree", "text": degree["text"], "blocking": True,
                    "label": f"asks for {DEGREE_NAMES[degree['level']]}"})
    blocking = [k for k in out if k["blocking"]]
    if blocking:
        adjustments.append({"kind": "knockout", "points": -min(20, 10 * len(blocking)),
                            "reason": "Hard requirement you don't meet: " + "; ".join(k["label"] for k in blocking) + "."})
    return out


def _requirement_lines(sections):
    for kind, text in sections:
        if kind in ("required", "preferred"):
            for line in text.split("\n"):
                line = re.sub(r"^[\s•\-*·▪]+", "", line).strip()
                if 15 <= len(line) <= 400:
                    yield kind, line


def _evidence(sections, kws, bullets, resume_text, limit=14):
    """Each requirement line of the posting, and the resume line that best shows it."""
    bullet_words = [(b, set(_tokens(b))) for b in bullets]
    out = []
    for kind, line in _requirement_lines(sections):
        line_kws = [c for c, k in kws.items() if has_any(k["aliases"], line)]
        have = [c for c in line_kws if has_any(kws[c]["aliases"], resume_text)]
        words = set(_tokens(line))
        best, best_score = "", 0
        for b, bw in bullet_words:
            s = 3 * sum(1 for c in line_kws if has_any(kws[c]["aliases"], b)) + len(words & bw)
            if s > best_score:
                best, best_score = b, s
        if line_kws:
            status = "met" if len(have) == len(line_kws) else "partial" if have else "missing"
        else:
            status = "likely" if best_score >= 3 else "unclear"
        out.append({"text": line, "kind": kind, "status": status, "keywords": line_kws, "have": have,
                    "evidence": best if best_score >= 2 else ""})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- the score
def score(resume_text: str, jd_text: str, title: str = "", extra=(), company: str = "", profile=None):
    """profile: app.candidate.profile(...) of the master resume; without it there are no adjustments."""
    sections = jd_sections(jd_text)
    relevant = section_text(sections) or jd_text
    kws = extract_keywords(jd_text, title, extra, company, sections)
    total = sum(k["weight"] for k in kws.values())
    matched, missing = [], []
    for canon, k in sorted(kws.items(), key=lambda kv: -kv[1]["weight"]):
        if has_any(k["aliases"], resume_text):
            matched.append(canon)
        else:
            missing.append({"keyword": canon, "count": k["count"], "weight": k["weight"], "where": k["where"],
                            "where_label": WHERE_LABEL.get(k["where"], ""), "context": _context(k["aliases"], jd_text)})
    coverage = (sum(kws[m]["weight"] for m in matched) / total) if total else 0.0
    sim = min(1.0, cosine(resume_text, relevant) / 0.45)
    tit = title_alignment(resume_text, title)
    base = 100 * (0.60 * coverage + 0.25 * sim + 0.15 * tit)

    adjustments, experience, seniority, knockouts, evidence = [], None, None, [], []
    req = requirements(sections, jd_text)
    if profile:
        experience = _experience(req, profile, adjustments)
        seniority = _seniority(title, profile, adjustments)
        knockouts = _knockouts(req, profile, adjustments)
        evidence = _evidence(sections, kws, profile.get("bullets") or [], resume_text)
    value = round(base + sum(a["points"] for a in adjustments))
    return {
        "score": max(0, min(100, value)),
        "base": round(base),
        "coverage": round(coverage * 100),
        "similarity": round(sim * 100),
        "title_alignment": round(tit * 100),
        "matched": matched,
        "missing": missing[:30],
        "aliases": {c: k["aliases"] for c, k in kws.items()},
        "where": {c: k["where"] for c, k in kws.items()},
        "adjustments": adjustments,
        "experience": experience,
        "seniority": seniority,
        "knockouts": knockouts,
        "evidence": evidence,
        "requirements": {"years": req["years"], "degree": req["degree"]},
        "boilerplate_share": round(100 * sum(len(t) for k, t in sections if k in WEIGHTLESS)
                                   / max(1, sum(len(t) for _, t in sections))),
    }
