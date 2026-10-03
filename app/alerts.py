"""New-match alerts: strong matches that scheduled saved searches found while you weren't looking.

They show up in the page (and as desktop notifications when you allow them), and each day's alerts are also written to
<personal folder>/Alerts/new-matches-YYYY-MM-DD.html, a page you can open even when Job Agent isn't running.
"""
import html
from datetime import date

from . import db
from .config import ALERTS


def digest_path(day: date | None = None):
    return ALERTS / f"new-matches-{(day or date.today()).isoformat()}.html"


def write_digest(day: date | None = None):
    """(Re)write the digest of one day's alerts. Returns its path, or None when that day has none."""
    day = day or date.today()
    rows = [a for a in db.alerts(unseen_only=False, since=day.isoformat(), limit=1000)
            if a["created_at"][:10] == day.isoformat()]
    if not rows:
        return None
    ALERTS.mkdir(parents=True, exist_ok=True)
    e = html.escape

    def salary(a):
        if not a.get("salary_max"):
            return ""
        return f" · ${a['salary_min'] / 1000:,.0f}k–${a['salary_max'] / 1000:,.0f}k"

    items = "".join(
        f"<tr><td class='s'>{a['score']}</td><td><a href='{e(a['url'] or '')}'>{e(a['title'] or '')}</a>"
        f"<div class='m'>{e(a['company'] or '')} · {e(a['location'] or '')}{e(salary(a))}</div></td>"
        f"<td class='m'>{e(a['search_name'] or '')}</td><td class='m'>{e(a['created_at'][11:16])}</td></tr>"
        for a in rows)
    path = digest_path(day)
    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>New matches {day.isoformat()}</title>
<style>body{{font:15px/1.45 system-ui,Segoe UI,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#0f172a}}
table{{border-collapse:collapse;width:100%}}td{{padding:8px 6px;border-bottom:1px solid #e2e8f0;vertical-align:top}}
.s{{font-weight:700;font-size:18px;color:#15803d;width:3em}}.m{{color:#475569;font-size:13px}}a{{color:#2563eb}}</style>
</head><body><h1>New matches · {day.strftime('%B %d, %Y').replace(' 0', ' ')}</h1>
<p class="m">Found by your scheduled saved searches. Open Job Agent to tailor a resume and apply.</p>
<table>{items}</table></body></html>""", encoding="utf-8")
    return path
