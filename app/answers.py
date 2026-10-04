"""The answer bank: your answers to the custom questions on application forms.

Every company asks its own questions ("Are you willing to relocate?", "Desired salary", "Why do you want to work
here?"). When you answer one in the apply window, Job Agent remembers the question and your answer. The next form that
asks the same question, or one worded almost the same, gets your answer filled in and outlined amber so you check it.
Settings > Answer bank lists them to edit or delete. The cover letter's short answers are added too (as drafts).

Matching happens here, not in the page: the page sends the questions it shows and gets back only the answers to those.

Never remembered: what your profile already fills (name, contact details, address), passwords and signatures, and
voluntary disclosures (gender, ethnicity, veteran status, disability), which Settings > Voluntary disclosures handles.
"""
import re
from difflib import SequenceMatcher

from . import db
from .jobparse import NON_US_COUNTRIES

KINDS = ("text", "choice")
SENSITIVE = re.compile(
    r"gender|\bsex\b|ethnic|\brace\b|hispanic|latino|veteran|disabilit|sexual orientation|pronoun|transgender|"
    r"date of birth|\bbirth|\bage\b|social security|\bssn\b|password|signature|marital|religio|"
    r"\bdate\b|today|initials", re.I)  # dates and initials are for this form only (often a signature)
PROFILE_LABEL = re.compile(r"\b(name|e-?mail|phone|address|city|postal|zip|state|province|country|county|"
                           r"linkedin|github|website|portfolio|url)\b", re.I)
STOP = set("a an the at for in on to of your you are is do does did please our with this that we be will would "
           "currently".split())
# Words that change what a question asks: "authorized to work in the US" is not "...in Canada".
CRITICAL = {"not", "no", "never", "future", "now", "past", "previously", "former", "us", "usa", "united", "states",
            "america", "american", *NON_US_COUNTRIES}


def normalize(question: str, company: str = "", title: str = "") -> str:
    """Lowercase words only, with this posting's company and job title replaced by {company} and {title}, so
    "Why do you want to work at Acme?" and "...at Globex?" are the same question."""
    q = (question or "").lower()
    for name, token in ((company, "{company}"), (title, "{title}")):
        name = (name or "").strip().lower()
        if len(name) >= 2:
            q = re.sub(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", f" {token} ", q)
    q = re.sub(r"\((required|optional)\)|\*", " ", q)
    q = re.sub(r"[^a-z0-9{}%$ ]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _content(norm: str) -> set:
    return {w for w in norm.split() if w not in STOP}


def similarity(a: str, b: str) -> float:
    """1.0 for the same question; otherwise high only when the wording differs in filler words, and never when a
    word that changes the meaning (a country, "not", "future", a number) differs."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ca, cb = _content(a), _content(b)
    if not ca or not cb:
        return 0.0
    diff = ca ^ cb
    if any(w in CRITICAL or w.isdigit() for w in diff):
        return 0.0
    if not diff:
        return 0.97
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio if len(diff) <= 1 else min(ratio, 0.8)


MATCH = 0.9


def rememberable(question: str, answer: str) -> bool:
    q, a = (question or "").strip(" *:?\t"), (answer or "").strip()
    if len(q) < 8 or not a or len(a) > 4000 or len(q) > 500:
        return False
    if SENSITIVE.search(q):
        return False
    if len(q) <= 40 and PROFILE_LABEL.search(q):
        return False  # a short contact/address label ("Address Line 1", "Phone Number"): the profile fills those
    return not re.fullmatch(r"(select one|select|choose one|-+|none selected)", a, re.I)


def capture(question, answer, kind="text", company="", title="", source="captured", replace=True):
    """Remember an answer. replace=False keeps an existing answer to the same question (cover-letter drafts never
    overwrite an answer you gave on a form). Returns the answer's id, or None when it isn't kept."""
    if not rememberable(question, answer):
        return None
    norm = normalize(question, company, title)
    if not norm:
        return None
    if not replace:
        existing = next((a for a in db.answers_all() if a["norm"] == norm), None)
        if existing and existing["source"] != source:
            return None
    return db.answer_upsert(question.strip(), norm, answer.strip(), kind if kind in KINDS else "text", source,
                            company or None)


def lookup(questions, company="", title=""):
    """For each question, the closest saved answer, or None: {id, question, answer, kind, exact, note}."""
    bank = db.answers_all()
    out = []
    for q in list(questions or [])[:60]:
        q = str(q or "")[:500]
        norm = normalize(q, company, title)
        best, score = None, 0.0
        for a in bank:
            s = similarity(norm, a["norm"])
            if s > score:
                best, score = a, s
        if not best or score < MATCH or SENSITIVE.search(q):
            out.append(None)
            continue
        note = "from your answer bank" if score == 1.0 else f"from your answer to “{best['question']}”"
        if best["company"] and company and best["company"].lower() != company.lower() \
                and best["company"].lower() in best["answer"].lower():
            note += f", written for {best['company']}"
        if best["source"] == "cover letter":
            note += " (a cover-letter draft)"
        out.append({"id": best["id"], "question": best["question"], "answer": best["answer"], "kind": best["kind"],
                    "exact": score == 1.0, "note": note})
    return out


def seed_from_letter(letter, company="", title=""):
    """Add the cover letter's short answers to the bank (without replacing answers you gave on a form)."""
    added = 0
    for a in (letter or {}).get("answers") or []:
        if capture(a.get("question"), a.get("text"), "text", company, title, source="cover letter", replace=False):
            added += 1
    return added
