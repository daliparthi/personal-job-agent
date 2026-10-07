"""Turn raw Workday postings into clean, filterable fields (text, salary, job type, remote, US state)."""
import re
from html.parser import HTMLParser

# ---------------------------------------------------------------- HTML -> text
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "table"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html or "")
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


# ---------------------------------------------------------------- "Posted 3 Days Ago"
def posted_days_ago(posted_on: str):
    s = (posted_on or "").lower()
    if "today" in s or "just" in s or "hour" in s:
        return 0
    if "yesterday" in s:
        return 1
    m = re.search(r"(\d+)\+?\s*day", s)
    if m:
        return int(m.group(1)) + (1 if "+" in s else 0)
    return None


# ---------------------------------------------------------------- salary
_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?([kK])?"
# Handles "$120,000 - $150,000", "$120K-$150K", "200,000 USD - 322,000 USD", "$136,900.00-193,270.00 USD".
_RANGE = re.compile(r"(\$\s?)?" + _NUM + r"\s*(USD)?\s*(?:-|–|—|to|and)\s*(\$\s?)?" + _NUM + r"\s*(USD)?(?=([^\n.;]{0,40}))")
_HOURLY = re.compile(r"hour|/\s?hr\b|\bhr\b|hourly", re.I)


def _amount(num, k):
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def parse_salary(text: str):
    """Return (annual_min, annual_max, snippet) from pay-transparency text, or (None, None, '')."""
    lows, highs, snippets = [], [], []
    for m in _RANGE.finditer(text or ""):
        if not (m.group(1) or m.group(4) or m.group(5) or m.group(8)):
            continue  # no currency marker: probably years or headcounts
        lo, hi = _amount(m.group(2), m.group(3)), _amount(m.group(6), m.group(7))
        if hi < lo:
            lo, hi = hi, lo
        context = text[max(0, m.start() - 60):m.end()]
        hourly = bool(_HOURLY.search(m.group(9) or "")) or (hi < 500 and bool(_HOURLY.search(context)))
        if hourly:
            if not (7 <= lo <= 1000 and 7 <= hi <= 1000):
                continue
            lo, hi = lo * 2080, hi * 2080
        elif not (15_000 <= lo <= 2_000_000 and 15_000 <= hi <= 2_000_000):
            continue
        lows.append(lo)
        highs.append(hi)
        snippets.append(m.group(0).strip())
    if not lows:
        return None, None, ""
    return min(lows), max(highs), snippets[0][:80]


# ---------------------------------------------------------------- employment type
def classify_employment(worker_sub_type: str, time_type: str, title: str, text: str) -> str:
    part_time = "part" in (time_type or "").lower()
    s = (worker_sub_type or "").lower()
    if s:
        if re.search(r"intern|student|co-?op|apprentic", s):
            return "Internship"
        if re.search(r"contract|contingent|consultant|freelanc", s):
            return "Contract"
        if re.search(r"\btemp|seasonal|fixed[ -]?term|casual|limited term", s):
            return "Temporary"
        return "Part-time" if part_time else "Full-time"
    t = (title or "").lower()
    if re.search(r"\bintern(ship)?\b|\bco-?op\b", t):
        return "Internship"
    if re.search(r"\bcontract(or)?\b|contract[- ]to[- ]hire|\bc2h\b", t):
        return "Contract"
    if re.search(r"\btemp(orary)?\b|\bseasonal\b|\bfixed[- ]term\b", t):
        return "Temporary"
    body = (text or "").lower()
    if re.search(r"\bthis is a (\w+ )?contract\b|\bcontract (position|role|assignment|opportunity)\b"
                 r"|\bcontract[- ]to[- ]hire\b|\b\d+[- ](month|week)s? contract\b", body):
        return "Contract"
    if re.search(r"\b(temporary|seasonal|fixed[- ]term) (position|role|assignment|opportunity)\b", body):
        return "Temporary"
    return "Part-time" if part_time else "Full-time"


# ---------------------------------------------------------------- remote / hybrid
def classify_remote(remote_type: str, locations, title: str, text: str) -> str:
    rt = (remote_type or "").lower()
    locs = " | ".join(locations or []).lower() + " " + (title or "").lower()
    if "hybrid" in rt or "flex" in rt:
        return "Hybrid"
    if "remote" in rt or "virtual" in rt or "home" in rt:
        return "Remote"
    if re.search(r"\bremote\b|\bvirtual\b|work from home|telecommut", locs):
        return "Remote"
    if "office" in rt or "site" in rt:
        return "On-site"
    body = (text or "").lower()
    if re.search(r"\b(fully|100%) remote\b|\bremote (position|role|opportunity)\b|\bthis (role|position) is remote\b"
                 r"|\bwork remotely from anywhere\b", body):
        return "Remote"
    if re.search(r"\bhybrid (work|role|schedule|model|position|arrangement)\b|\bthis (role|position) is hybrid\b", body):
        return "Hybrid"
    if re.search(r"\b(on-?site|in[- ]office) (role|position|requirement)\b|\b\d days? (a|per) week in (the )?office\b", body):
        return "On-site" if "hybrid" not in body else "Hybrid"
    return "Unspecified"


# ---------------------------------------------------------------- US states
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_NAME_TO_CODE = {v.lower(): k for k, v in US_STATES.items()}

# Big employer cities, for postings that only give a city ("San Jose", "Austin").
CITY_STATE = {
    "new york": "NY", "new york city": "NY", "nyc": "NY", "brooklyn": "NY", "manhattan": "NY", "buffalo": "NY",
    "rochester": "NY", "albany": "NY", "los angeles": "CA", "san francisco": "CA", "san jose": "CA",
    "santa clara": "CA", "sunnyvale": "CA", "mountain view": "CA", "palo alto": "CA", "menlo park": "CA",
    "cupertino": "CA", "san diego": "CA", "irvine": "CA", "oakland": "CA", "sacramento": "CA", "fremont": "CA",
    "milpitas": "CA", "redwood city": "CA", "san mateo": "CA", "pleasanton": "CA", "folsom": "CA",
    "santa monica": "CA", "burbank": "CA", "el segundo": "CA", "long beach": "CA", "san ramon": "CA",
    "seattle": "WA", "bellevue": "WA", "redmond": "WA", "kirkland": "WA", "tacoma": "WA", "spokane": "WA",
    "portland": "OR", "hillsboro": "OR", "beaverton": "OR", "austin": "TX", "dallas": "TX", "houston": "TX",
    "san antonio": "TX", "plano": "TX", "irving": "TX", "fort worth": "TX", "frisco": "TX", "round rock": "TX",
    "richardson": "TX", "chicago": "IL", "boston": "MA", "cambridge": "MA", "burlington": "MA",
    "waltham": "MA", "atlanta": "GA", "alpharetta": "GA", "denver": "CO", "boulder": "CO",
    "colorado springs": "CO", "phoenix": "AZ", "chandler": "AZ", "tempe": "AZ", "scottsdale": "AZ",
    "tucson": "AZ", "miami": "FL", "orlando": "FL", "tampa": "FL", "jacksonville": "FL",
    "fort lauderdale": "FL", "charlotte": "NC", "raleigh": "NC", "durham": "NC", "research triangle park": "NC",
    "morrisville": "NC", "cary": "NC", "nashville": "TN", "memphis": "TN", "minneapolis": "MN",
    "st. paul": "MN", "saint paul": "MN", "detroit": "MI", "ann arbor": "MI", "philadelphia": "PA",
    "pittsburgh": "PA", "columbus": "OH", "cleveland": "OH", "cincinnati": "OH", "indianapolis": "IN",
    "salt lake city": "UT", "lehi": "UT", "draper": "UT", "las vegas": "NV", "reno": "NV",
    "baltimore": "MD", "bethesda": "MD", "arlington": "VA", "reston": "VA", "mclean": "VA", "herndon": "VA",
    "richmond": "VA", "chantilly": "VA", "kansas city": "MO", "st. louis": "MO", "saint louis": "MO",
    "omaha": "NE", "milwaukee": "WI", "madison": "WI", "new orleans": "LA", "louisville": "KY",
    "oklahoma city": "OK", "tulsa": "OK", "albuquerque": "NM", "boise": "ID", "honolulu": "HI",
    "anchorage": "AK", "hartford": "CT", "stamford": "CT", "providence": "RI", "newark": "NJ",
    "jersey city": "NJ", "princeton": "NJ", "wilmington": "DE", "des moines": "IA", "birmingham": "AL",
    "huntsville": "AL", "little rock": "AR", "charleston": "SC", "greenville": "SC", "columbia": "SC",
    "washington dc": "DC", "washington, d.c.": "DC", "washington d.c.": "DC",
}

NON_US_COUNTRIES = {
    "india", "china", "israel", "germany", "united kingdom", "uk", "canada", "mexico", "brazil", "japan",
    "france", "ireland", "netherlands", "singapore", "australia", "taiwan", "korea", "south korea", "vietnam",
    "malaysia", "poland", "spain", "italy", "switzerland", "sweden", "denmark", "finland", "norway",
    "belgium", "austria", "czechia", "czech republic", "romania", "hungary", "costa rica", "argentina",
    "chile", "colombia", "philippines", "indonesia", "thailand", "hong kong", "armenia", "portugal",
    "turkey", "egypt", "south africa", "new zealand", "united arab emirates", "uae", "saudi arabia",
    "ind", "chn", "isr", "deu", "gbr", "can", "mex", "bra", "jpn", "fra", "irl", "nld", "sgp", "aus",
    "twn", "kor", "vnm", "mys", "pol", "esp", "ita", "che", "swe", "cri", "phl",
}


def states_in(location: str) -> set:
    """US state codes mentioned in one Workday location string."""
    loc = (location or "").strip()
    if not loc:
        return set()
    low = loc.lower()
    if re.search(r"\bd\.?c\.?\b", low) and "washington" in low:
        return {"DC"}
    found = set()
    for name, code in _NAME_TO_CODE.items():
        if re.search(rf"\b{re.escape(name)}\b", low):
            found.add(code)
    if "west virginia" in low and not re.search(r"(?<!west )virginia", low):
        found.discard("VA")
    for tok in re.split(r"[,\-–|/()]+|\s{2,}", loc):
        tok = tok.strip()
        if len(tok) == 2 and tok.isupper() and tok in US_STATES:
            found.add(tok)
    if not found:
        for part in re.split(r"[,\-–|/()]+", low):
            code = CITY_STATE.get(part.strip())
            if code:
                found.add(code)
    return found


def looks_non_us(location_text: str) -> bool:
    """Cheap pre-filter on list results so we skip detail calls for obviously foreign postings."""
    first = re.split(r"[,\-–|]", (location_text or "").lower())[0].strip()
    return first in NON_US_COUNTRIES


def is_us_location(location: str) -> bool:
    low = (location or "").lower()
    return bool(states_in(location)) or bool(re.search(r"\b(usa|united states|u\.s\.)\b|^us\b|\bus,|us - ", low))


# ---------------------------------------------------------------- keywords
def split_keywords(raw: str):
    return [k.strip() for k in re.split(r"[,;\n]", raw or "") if k.strip()]


def kw_pattern(kw: str):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(kw.strip()) + r"(?![A-Za-z0-9])", re.I)


def contains_all(text: str, keywords) -> bool:
    return all(kw_pattern(k).search(text) for k in keywords)


def contains_any(text: str, keywords) -> bool:
    return any(kw_pattern(k).search(text) for k in keywords)


def keywords_present(text: str, keywords):
    return [k for k in keywords if kw_pattern(k).search(text)]


# ---------------------------------------------------------------- job description sections
# A posting is split by its headings into: intro (before any heading), required, preferred, responsibilities,
# boilerplate (about the company, benefits, pay, EEO, accommodations...) and other (a heading we don't recognise).
# The score weighs keywords by where they appear: "required" counts most, boilerplate not at all.
_H = [
    ("preferred", r"(preferred|desired|desirable|bonus|helpful|additional|optional)\b.*|nice[- ]to[- ]haves?.*"
                  r"|(ways to )?stand out.*|even better if.*|it would be (great|nice).*|good to have.*|pluses|a plus"
                  r"|what (would|will) make you stand out.*|extra credit.*"),
    ("responsibilities", r"(key |core |main |primary |your )?(responsibilities|duties)( include)?"
                         r"|what you('ll| will)( actually)?( be)? (do|doing|build|building|own|work on)\b.*"
                         r"|(about|the) (the )?(role|position|job|opportunity)|role (description|overview|summary)"
                         r"|job (description|summary|overview)|your (role|impact|day[- ]to[- ]day|mission)"
                         r"|in this role.*|day[- ]to[- ]day.*|the impact you('ll| will) (make|have)|what's the role"),
    ("required", r"(minimum|basic|required|essential|key|must[- ]have)\b.*|requirements?|qualifications?"
                 r"|what we need to see|what you('ll)? need.*|what (you|you'll) bring.*|what we('re| are) looking for.*"
                 r"|who you are|about you|you (have|bring|are)\b.*|you('re| are) (our|a great|the right) .*\bif\b.*"
                 r"|(skills|experience)( (and|&) (skills|experience|qualifications))?|the experience( you need)?"
                 r"|your (skills|experience|background|qualifications)|what you should have|you should have.*"
                 r"|to (be successful|succeed).*|(experience|skills|qualifications) (required|needed)"
                 r"|required (experiences?|skills) (&|and) (skills|experiences?)"),
    ("other", r"about (the|our) team|the team|team (overview|description)"),
    ("boilerplate", r"about (us|[a-z0-9&.,' -]{2,40})|(our |the )?(benefits|perks|compensation|total rewards)\b.*"
                    r"|(pay|salary|compensation) (range|transparency|information|details).*|base (pay|salary).*"
                    r"|(equal (employment )?opportunity|eeo|diversity|inclusion|accommodations?|privacy|disclaimer)\b.*"
                    r"|why (join|work).*|life at .*|we offer.*|what we offer.*|our (company|culture|commitment|values|mission)"
                    r"|job (details|category|posting|id)|posting statement|unleash your potential|location|req(uisition)? id"
                    r"|how to apply|additional information|important information"),
]
_HEADINGS = [(kind, re.compile(rx)) for kind, rx in _H]
# Sentences that are boilerplate wherever they appear (an EEO paragraph without a heading, a pay-range line...).
_BOILER_LINE = re.compile(
    r"equal (employment )?opportunity|without regard to|regardless of (race|age|gender|sex|religion|color)"
    r"|reasonable accommodations?|\b(base )?(pay|salary|compensation) range|\b401\(?k\)?|medical, dental|paid time off"
    r"|e-?verify|pay transparency|we are proud to be|protected (veteran|characteristic)|background check", re.I)
WEIGHTLESS = ("boilerplate",)


def _heading_kind(line: str):
    s = line.strip()
    if not s or len(s) > 80 or s[0] in "•-*·▪" or s.endswith("."):
        return None  # a bullet or a sentence ("Minimum of five years of experience.") is not a heading
    if len(s.split()) > (12 if s.endswith(":") else 7):
        return None
    t = re.sub(r"[\s:!?.…]+$", "", s.replace("’", "'").replace("‘", "'")).strip().lower()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None
    for kind, rx in _HEADINGS:
        if rx.fullmatch(t):
            return kind
    if s.endswith(":") and len(t.split()) <= 8:
        return "other"
    return None


def jd_sections(text: str):
    """[(kind, text)] in order. kind: intro, required, preferred, responsibilities, boilerplate or other."""
    out = []
    kind = "intro"
    for line in (text or "").split("\n"):
        h = _heading_kind(line)
        if h:
            kind = h
            continue
        if not line.strip():
            continue
        k = "boilerplate" if _BOILER_LINE.search(line) else kind
        if out and out[-1][0] == k:
            out[-1] = (k, f"{out[-1][1]}\n{line}")
        else:
            out.append((k, line))
    return out


def section_text(sections, kinds=None, exclude=WEIGHTLESS) -> str:
    return "\n".join(t for k, t in sections if (kinds is None or k in kinds) and k not in exclude)


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+|\n", text or "") if s.strip()]


_NOT_ABOUT = re.compile(r"\b(apply|applying|application|applicants?|candidates?|please|click|resume|cv|recruit\w*)\b", re.I)


_ABOUT_HEADING = re.compile(r"^\W*(about\b|who we are|our (company|mission|story|team)|the (company|team)\b|"
                            r"company (overview|description|profile))", re.I)


def _about_text(text: str) -> str:
    """The text before the first heading, plus any "About <company>" / "Who we are" section (which scoring counts
    as boilerplate)."""
    out, take = [], True
    for line in (text or "").split("\n"):
        if _heading_kind(line):
            take = bool(_ABOUT_HEADING.match(line))
        elif take:
            out.append(line)
    return "\n".join(out)


def jd_digest(text: str, about=3, duties=4) -> dict:
    """The posting's own words for the cover letter and short answers: the first sentences about the company (or
    team), and the first listed duties. Pay, benefits, EEO text and how-to-apply instructions are left out."""
    out = {"about": [], "duties": []}
    for s in _sentences(_about_text(text)):
        s = s.strip(" •-*·▪\t")
        if 40 <= len(s) <= 300 and s[-1:] in ".!" and not _BOILER_LINE.search(s) and not _NOT_ABOUT.search(s):
            out["about"].append(s)
            if len(out["about"]) >= about:
                break
    for line in section_text(jd_sections(text), ("responsibilities",)).split("\n"):
        line = line.strip(" •-*·▪\t")
        if 15 <= len(line) <= 240 and not line.endswith(":"):
            out["duties"].append(line)
            if len(out["duties"]) >= duties:
                break
    return out


# "...preferred", "...is a plus": a wish, not a requirement
_WISH = re.compile(r"\b(preferred|a plus|is a bonus|nice[- ]to[- ]have|desired|desirable|ideally)\b", re.I)


# ---------------------------------------------------------------- what the posting requires
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20}
_N = r"(\d{1,2}|" + "|".join(_NUM_WORDS) + r")"
YEARS_RE = re.compile(
    r"(?:(?:at\s+least|minimum(?:\s+of)?|min\.?|over|more\s+than|a\s+minimum\s+of)\s+)?" + _N +
    r"\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*" + _N + r"\s*\+?\s*)?(?:\(\s*\d+\s*\)\s*)?(?:years?|yrs?)\b", re.I)
_EXPERIENCE_WORDS = re.compile(r"experien|\bexp\b|background|track record|working (with|in|on)|hands-on|developing|"
                               r"building|industry|professional|relevant", re.I)


def _num(s):
    return int(s) if s.isdigit() else _NUM_WORDS.get(s.lower(), 0)


def years_required(sections):
    """Years-of-experience requirements: [{years, kind, text, after}], `after` being the words that follow the
    number (the caller finds the skill it is about there). Only sentences about experience; never boilerplate."""
    out = []
    for kind, text in sections:
        if kind in WEIGHTLESS:
            continue
        for sentence in _sentences(text):
            for m in YEARS_RE.finditer(sentence):
                years = _num(m.group(1))
                if not 1 <= years <= 30:
                    continue
                after = sentence[m.end(): m.end() + 120]
                # "...years of experience", or terse: "5+ years with Kafka", "3 years in Python", "2 years using dbt"
                if not (_EXPERIENCE_WORDS.search(sentence[max(0, m.start() - 50): m.end() + 90])
                        or re.match(r"\s*(with|in|using|working)\b", after, re.I)):
                    continue
                if re.search(r"\b(ago|old|history|founded|warranty)\b", sentence[m.end(): m.end() + 25], re.I):
                    continue
                out.append({"years": years, "kind": kind, "text": sentence.strip()[:240], "after": after})
    return out


DEGREE_LEVELS = [(3, r"ph\.?\s?d\.?|doctorate|doctoral"),
                 (2, r"master'?s|m\.\s?s\.?|\bms\b|m\.?sc|mba|graduate degree"),
                 (1, r"bachelor'?s|b\.\s?s\.?|\bbs\b|b\.?sc|b\.\s?a\.?|\bba\b|undergraduate degree|(4|four)[- ]year degree"
                     r"|b\.?\s?tech|b\.?e\.(?=\s)")]
DEGREE_NAMES = {1: "a bachelor's degree", 2: "a master's degree", 3: "a PhD"}
_DEGREE_RX = [(lvl, re.compile(rf"(?<![a-z]){rx}", re.I)) for lvl, rx in DEGREE_LEVELS]
_EQUIVALENT = re.compile(r"or (equivalent|comparable|related) (practical |work |professional |industry )?experience"
                         r"|equivalent experience|or equivalent|in lieu of"
                         r"|experience\W+(\w+\W+){0,3}or\s+(an?\s+)?(master|bachelor|ph\.?\s?d|degree)", re.I)


def degree_level(text: str):
    """Highest degree named in a piece of text (1 bachelor, 2 master, 3 PhD), or None."""
    found = [lvl for lvl, rx in _DEGREE_RX if rx.search((text or "").replace("’", "'"))]
    return max(found) if found else None


def degree_required(sections):
    """The lowest degree a posting requires ({level, text}), or None. "...or equivalent experience" doesn't count."""
    for kind, text in sections:
        if kind not in ("required", "intro", "other"):
            continue
        for sentence in _sentences(text):
            s = sentence.replace("’", "'")
            levels = [lvl for lvl, rx in _DEGREE_RX if rx.search(s)]
            if not levels or not re.search(r"degree|\bin (computer|engineering|math|statistics|science|a related)", s, re.I):
                continue
            if _EQUIVALENT.search(s) or _WISH.search(s):
                continue
            return {"level": min(levels), "text": sentence.strip()[:240]}
    return None


KNOCKOUT_PATTERNS = [
    ("sponsorship", re.compile(
        r"(not|unable to|cannot|can't|won't|will not|does not|doesn't|do not|no)\s+(be\s+)?(able to\s+)?"
        r"(provide|offer|support|consider)?\s*(employment\s+)?(visa\s+|immigration\s+|work\s+)?sponsor"
        r"|without (the need for |requiring )?(current or future |future |any )?(employment |visa |immigration )?sponsorship"
        r"|sponsorship (is |will )?not (be )?(available|offered|provided)|not eligible for (visa )?sponsorship", re.I)),
    ("citizenship", re.compile(
        r"(must be|requires?|required to be|only|limited to)\s+(an?\s+)?(u\.?s\.?|united states)\s+citizens?"
        r"|(u\.?s\.?|united states) citizenship (is )?(required|needed|mandatory)|us citizens only", re.I)),
    ("clearance", re.compile(
        r"(active|current|existing|ability to obtain|able to obtain|must (have|hold|possess|obtain|maintain)"
        r"|eligib\w+ (for|to obtain))\s+(an?\s+)?(u\.?s\.?\s+)?(government\s+)?"
        r"(secret|top secret|ts/sci|ts|security|public trust|dod)(\s+security)?\s+clearance"
        r"|(secret|ts/sci|top secret|security) clearance (is )?(required|needed)", re.I)),
]


def knockouts_in(text: str):
    """Hard requirements a posting states anywhere: [{kind, text}] for sponsorship, citizenship and clearance."""
    out = []
    for kind, rx in KNOCKOUT_PATTERNS:
        for m in rx.finditer(text or ""):
            start = text.rfind("\n", 0, m.start()) + 1
            end = text.find("\n", m.end())
            line = text[start: end if end > 0 else len(text)]
            # Only the sentence the match is in: "...active security clearance preferred" is a wish.
            a, b = m.start() - start, m.end() - start
            s_start = max(line.rfind(". ", 0, a), line.rfind("; ", 0, a)) + 1
            ends = [i for i in (line.find(". ", b), line.find("; ", b)) if i >= 0]
            if _WISH.search(line[s_start: min(ends) if ends else len(line)]):
                continue
            out.append({"kind": kind, "text": line.strip()[:240]})
            break
    return out


_SPONSOR_NO = KNOCKOUT_PATTERNS[0][1]
_SPONSOR_YES = re.compile(
    r"sponsorship\s+(is\s+|will\s+be\s+|may\s+be\s+)?(available|offered|provided|possible|considered)"
    r"|(will|can|may|able to|happy to|willing to|open to|we)\s+(also\s+)?(provide\s+|offer\s+|consider\s+)?"
    r"(visa\s+|immigration\s+|work\s+|employment\s+)?sponsor"
    r"|we\s+(do\s+)?sponsor|sponsor(s|ing)?\s+(h-?1b|visas?|work\s+(visas?|authorization))"
    r"|visa\s+(support|assistance)|immigration\s+(support|assistance)", re.I)
_H1B = re.compile(r"\bh-?\s?1-?b\b", re.I)
_H4EAD = re.compile(r"\bh-?\s?4\b|\bead\b|employment authorization document", re.I)
_REFUSES = re.compile(r"\b(no|not|unable|cannot|can't|won't|don't|doesn't|unfortunately|without)\b", re.I)
SPONSORSHIP_LABELS = {"sponsors": "sponsors visas", "h1b": "H-1B", "h4ead": "H-4 EAD ok", "no": "no sponsorship"}


def sponsorship_in(text: str) -> list:
    """What a posting says about work authorization, as tags: "sponsors" (offers visa sponsorship), "h1b" (names
    H-1B as possible), "h4ead" (accepts H-4 EAD holders) and "no" (states it won't sponsor). A posting can carry
    several ("no" and "h4ead": no sponsorship, but EAD holders are welcome). [] when it says nothing."""
    tags = set()
    for sentence in _sentences(text or ""):
        if _SPONSOR_NO.search(sentence):
            tags.add("no")
            continue
        refuses = _REFUSES.search(sentence)
        if _SPONSOR_YES.search(sentence) and not refuses:
            tags.add("sponsors")
        if _H1B.search(sentence) and not refuses:
            tags.add("h1b")
        if _H4EAD.search(sentence) and not refuses:
            tags.add("h4ead")
    return [t for t in ("sponsors", "h1b", "h4ead", "no") if t in tags]


# ---------------------------------------------------------------- seniority
SENIORITY_LADDER = [  # checked top-down: "Senior Staff Engineer" is staff, "Associate Director" a director
    (7, r"\b(vp|svp|evp|vice president|chief|cto|cio|ciso|head of)\b"),
    (6, r"\bdirector\b"),
    (5, r"\b(senior|sr\.?) manager\b|\b(principal|distinguished|fellow)\b|\bgroup manager\b"),
    (4, r"\b(staff|lead|manager|architect)\b"),
    (3, r"\b(senior|sr\.?|iii|level 3)\b"),
    (0, r"\b(intern|internship|co-?op|apprentice)\b"),
    (1, r"\b(junior|jr\.?|entry[- ]level|associate|graduate|new grad|trainee|i)\b"),
    (2, r"\b(ii|mid[- ]level|intermediate)\b"),
]
SENIORITY_NAMES = {0: "an internship", 1: "a junior role", 2: "a mid-level role", 3: "a senior role",
                   4: "a staff / lead / manager role", 5: "a principal / senior manager role", 6: "a director role",
                   7: "an executive role"}
_LADDER = [(lvl, re.compile(rx, re.I)) for lvl, rx in SENIORITY_LADDER]


def seniority_level(title: str):
    """0 intern … 7 executive, from a job title; 2 (mid-level) when the title names no level; None for no title."""
    t = (title or "").strip()
    if not t:
        return None
    for lvl, rx in _LADDER:
        if rx.search(t):
            return lvl
    return 2
