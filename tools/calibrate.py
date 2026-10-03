"""Fit the match-score weights to your own outcomes.

Good matches: jobs you gave a thumbs up, or that reached a recruiter screen, interview or offer. Poor matches: a thumbs
down, or rejected / ghosted without ever getting a screen. For every split of the base weights (keyword coverage /
wording similarity / title alignment, in steps of 5) this measures how often a good match outscores a poor one (AUC),
and prints the best split next to the current 60/25/15. It changes nothing; the weights live in app/scoring.py.

    .venv\\Scripts\\python tools\\calibrate.py                  (your default personal folder)
    .venv\\Scripts\\python tools\\calibrate.py --profile Alex   (or --home PATH)
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURRENT = (60, 25, 15)
GOOD = {"screening", "interviewing", "offer"}
POOR = {"rejected", "ghosted"}


def auc(scored) -> float | None:
    """scored: [(score, is_good)]. The share of (good, poor) pairs where the good one scores higher (ties: half)."""
    good = [s for s, g in scored if g]
    poor = [s for s, g in scored if not g]
    if not good or not poor:
        return None
    wins = sum(1.0 if g > p else 0.5 if g == p else 0.0 for g in good for p in poor)
    return wins / (len(good) * len(poor))


def weight_splits(step=5):
    for a in range(0, 101, step):
        for b in range(0, 101 - a, step):
            yield a, b, 100 - a - b


def fit(rows, step=5):
    """rows: [(coverage, similarity, title_alignment, adjustment_points, is_good)] with the first three in 0..100.
    Returns [(auc, weights)] best first."""
    out = []
    for w in weight_splits(step):
        scored = [((w[0] * c + w[1] * s + w[2] * t) / 100 + adj, good) for c, s, t, adj, good in rows]
        value = auc(scored)
        if value is not None:
            out.append((round(value, 3), w))
    return sorted(out, key=lambda x: (-x[0], abs(x[1][0] - CURRENT[0]) + abs(x[1][1] - CURRENT[1])))


def label(job, reached) -> bool | None:
    if job.get("feedback") == 1:
        return True
    if job.get("feedback") == -1:
        return False
    if reached & GOOD:
        return True
    if reached & POOR:
        return False
    return None


def load_rows():
    from app import candidate, db, scoring
    from app.jobparse import split_keywords
    resume = db.get_resume()
    if not resume:
        sys.exit("No master resume in this personal folder.")
    settings = db.get_settings()
    prof = candidate.profile(resume["data"], settings["profile"])
    extra = split_keywords(settings["mandatory"]) + split_keywords(settings["optional"])
    reached = {}
    for job_id, to_status, _ in db.status_events():
        reached.setdefault(job_id, set()).add(to_status)
    rows = []
    for j in db.all_jobs(with_text=True):
        good = label(j, reached.get(j["id"], set()) | {j.get("status")})
        if good is None:
            continue
        sc = scoring.score(resume["text"], j["description_text"] or "", j["title"], extra, j["company"], prof)
        rows.append((sc["coverage"], sc["similarity"], sc["title_alignment"],
                     sum(a["points"] for a in sc["adjustments"]), good))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile")
    ap.add_argument("--home")
    args = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from app import config
    os.environ["JOB_AGENT_HOME"] = str(config.home_for(args.profile, args.home))
    import importlib
    importlib.reload(config)
    from app import db
    db.init()
    rows = load_rows()
    good = sum(1 for r in rows if r[-1])
    print(f"{len(rows)} labelled jobs: {good} good matches, {len(rows) - good} poor ones.")
    if good < 5 or len(rows) - good < 5:
        print("Too few to learn from: mark at least 5 of each (thumbs up/down, or move jobs along the pipeline).")
        return
    results = fit(rows)
    current = next((a for a, w in results if w == CURRENT), None)
    best_auc, best = results[0]
    print(f"Current weights {CURRENT}: AUC {current}")
    print(f"Best weights    {best}: AUC {best_auc}")
    for a, w in results[1:6]:
        print(f"                {w}: AUC {a}")
    if best != CURRENT and current is not None and best_auc - current >= 0.03:
        print("To use the best split, change the 0.60 / 0.25 / 0.15 in score() in app/scoring.py.")
    else:
        print("The current weights are about as good as any split for your outcomes.")


if __name__ == "__main__":
    main()
