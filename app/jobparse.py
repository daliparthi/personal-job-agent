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


def keywords_present(text: str, keywords):
    return [k for k in keywords if kw_pattern(k).search(text)]
