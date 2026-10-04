"""Helpers shared by the job-board modules."""
import re
from datetime import date, datetime, timezone


def when(value):
    """A posting date from an ISO timestamp or epoch milliseconds."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
        return date.fromisoformat(str(value)[:10])
    except (ValueError, OverflowError, OSError):
        return None


def per_year(lo, hi, interval: str):
    """(min, max) a year from a pay range and its interval ("1 YEAR", "per-hour-wage", "monthly", ...)."""
    if lo is None and hi is None:
        return None
    lo, hi = float(lo if lo is not None else hi), float(hi if hi is not None else lo)
    iv = (interval or "year").lower()
    factor = 2080 if "hour" in iv else 12 if "month" in iv else 52 if "week" in iv else 1
    lo, hi = lo * factor, hi * factor
    if not (15_000 <= lo <= 2_000_000 and 15_000 <= hi <= 2_000_000):
        return None
    return min(lo, hi), max(lo, hi)


def locations(*values) -> list:
    """Unique, non-empty location strings; "A; B" lists are split."""
    out = []
    for v in values:
        for part in re.split(r"\s*;\s*", v or "") if isinstance(v, str) else []:
            part = re.sub(r"\s+", " ", part).strip()
            if part and part not in out:
                out.append(part)
    return out
