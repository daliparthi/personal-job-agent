"""
ATS-style match score between a resume and a job description.

score = 60% weighted keyword coverage (what ATS filters look for)
      + 25% overall wording similarity (cosine over content words)
      + 15% job-title alignment (are the title's words in your resume?)
"""
import math
import re
from collections import Counter
from functools import lru_cache

from .lexicon import ACRONYM_STOP, CASE_SENSITIVE, ENTRIES

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


def _is_acronym(alias: str) -> bool:
    return " " not in alias and len(alias) <= 6 and sum(ch.isupper() for ch in alias) >= 2


@lru_cache(maxsize=None)
def _alias_regex(alias: str):
    flags = 0 if (alias in CASE_SENSITIVE or _is_acronym(alias)) else re.I
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9+#])", flags)


def _count(aliases, text) -> int:
    return sum(len(_alias_regex(a).findall(text)) for a in aliases)


def _has(aliases, text) -> bool:
    return any(_alias_regex(a).search(text) for a in aliases)


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


def extract_keywords(jd_text: str, title: str = "", extra=(), company: str = ""):
    """{canonical: {"aliases": [...], "count": n, "weight": w}} for every ATS keyword found in the JD."""
    found = {}
    known_aliases = set()
    company_words = set(re.findall(r"[a-z0-9]+", (company or "").lower()))
    for canon, aliases, base in ENTRIES:
        known_aliases.update(a.lower() for a in aliases)
        if any(a.lower() in company_words for a in aliases):
            continue  # the employer's own name is not a skill
        n = _count(aliases, jd_text)
        if n:
            w = base * min(3, n) + (1.5 if _has(aliases, title) else 0)
            found[canon] = {"aliases": aliases, "count": n, "weight": round(w, 2)}
    for acr, n in _acronyms(jd_text).items():
        if acr.lower() not in known_aliases and acr.lower() not in company_words and acr not in found:
            found[acr] = {"aliases": [acr], "count": n, "weight": round(min(3, n) * 0.8, 2)}
    for kw in extra:
        if not kw or kw.lower() in known_aliases or any(kw.lower() == c.lower() for c in found):
            # Already covered by the dictionary: just make sure it counts as important.
            for k in found.values():
                if kw and kw.lower() in (a.lower() for a in k["aliases"]):
                    k["weight"] = max(k["weight"], 2.0)
            continue
        if _has([kw], jd_text):
            found[kw] = {"aliases": [kw], "count": _count([kw], jd_text), "weight": 2.0}
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


def score(resume_text: str, jd_text: str, title: str = "", extra=(), company: str = ""):
    kws = extract_keywords(jd_text, title, extra, company)
    total = sum(k["weight"] for k in kws.values())
    matched, missing = [], []
    for canon, k in sorted(kws.items(), key=lambda kv: -kv[1]["weight"]):
        if _has(k["aliases"], resume_text):
            matched.append(canon)
        else:
            missing.append({"keyword": canon, "count": k["count"], "weight": k["weight"],
                            "context": _context(k["aliases"], jd_text)})
    coverage = (sum(kws[m]["weight"] for m in matched) / total) if total else 0.0
    sim = min(1.0, cosine(resume_text, jd_text) / 0.45)
    tit = title_alignment(resume_text, title)
    value = round(100 * (0.60 * coverage + 0.25 * sim + 0.15 * tit))
    return {
        "score": max(0, min(100, value)),
        "coverage": round(coverage * 100),
        "similarity": round(sim * 100),
        "title_alignment": round(tit * 100),
        "matched": matched,
        "missing": missing[:30],
        "aliases": {c: k["aliases"] for c, k in kws.items()},
    }
