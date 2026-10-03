"""Start Job Agent. Normally via start.bat (Windows), start.command (macOS, double-click) or ./start.sh
(macOS/Linux), which set up Python first and pass the same options:

    start                       your personal folder: <home>/JobAgent/default
    start --profile Alex        another person sharing this computer account: <home>/JobAgent/Alex
    start --home "/data/jobs"   keep your personal folder somewhere else
    start --no-browser          don't open a browser tab

Saved searches with a schedule run on their own while Job Agent is open. To run them while it is closed, let your
OS scheduler call  run.py --run-searches  (run.py --schedule-help prints the exact command for this computer).

Each person (each user account, or each --profile) gets their own data, port and access key, so one copy of
the source code serves everyone on the computer.
"""
import argparse
import asyncio
import importlib
import json
import os
import socket
import sys
import threading
import urllib.request
import webbrowser


def _server_home(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("home_id")
    except Exception:
        return None


def _port_free(host, port):
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _candidate_ports(config, preferred):
    candidates = []
    if config.PORT_FILE.exists():
        try:
            candidates.append(int(config.PORT_FILE.read_text().strip()))
        except ValueError:
            pass
    return candidates + [p for p in range(preferred, preferred + 30) if p not in candidates]


def headless(args, config, home, home_id, candidates):
    """--schedule-help / --run-searches: no server, no browser."""
    from app import db, scheduler
    if args.schedule_help:
        h = scheduler.schedule_help()
        print(f"Command that runs your due saved searches (personal folder {home}):\n  {h['command']}\n")
        print(f"{h['how']}\n  {h['register']}\n\n{h['note']}")
        return
    # A Job Agent already open for this folder runs its own schedule; don't run the same searches twice. A running
    # server always records its port in .port, so only that one needs asking (each refused port costs ~1.5 s).
    if config.PORT_FILE.exists() and _server_home(candidates[0]) == home_id:
        print("Job Agent is open for this personal folder and runs its scheduled searches itself.", flush=True)
        return
    db.init()
    try:
        results = asyncio.run(scheduler.run_due_once())
    finally:
        db.close_all()
    if not results:
        print("No saved search is due.")
    for r in results:
        errors = f", {len(r['errors'])} error(s)" if r["errors"] else ""
        print(f"{r['name']}: {r['new_jobs']} new, {r['refreshed']} refreshed, {r['alerts']} alert(s){errors}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="separate personal folder for another person (default: 'default')")
    ap.add_argument("--home", help="use this folder as the personal folder")
    ap.add_argument("--port", type=int, default=8765, help="preferred port (the next free one is used if taken)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--run-searches", action="store_true",
                    help="run the saved searches that are due, then exit (for Task Scheduler / cron)")
    ap.add_argument("--schedule-help", action="store_true",
                    help="print how to run saved searches while Job Agent is closed")
    args = ap.parse_args()

    # app.config reads the personal folder and port from the environment, so reload it after setting them.
    from app import config
    try:
        home = config.home_for(args.profile, args.home)
    except ValueError as e:
        sys.exit(str(e))
    os.environ["JOB_AGENT_HOME"] = str(home)
    importlib.reload(config)
    home_id = config.HOME_ID
    config.ensure_home()
    key = config.session_key()

    candidates = _candidate_ports(config, args.port)
    if args.schedule_help or args.run_searches:
        return headless(args, config, home, home_id, candidates)

    # Already running for this person? Just open it. Otherwise take the first free port.
    port = None
    for p in candidates:
        owner = _server_home(p)
        if owner == home_id:
            url = f"http://localhost:{p}/?key={key}"
            print(f"Job Agent is already running for {home}\nOpen: {url}", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return
        if owner is None and _port_free(config.HOST, p):
            port = p
            break
    if port is None:
        sys.exit("No free port found between %d and %d" % (args.port, args.port + 29))

    os.environ["JOB_AGENT_PORT"] = str(port)
    importlib.reload(config)
    config.PORT_FILE.write_text(str(port))
    url = f"http://localhost:{port}/?key={key}"
    print(f"Job Agent - personal folder: {home}")
    print(f"Open: {url}\n(keep this window open; Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
