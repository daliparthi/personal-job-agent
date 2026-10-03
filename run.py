"""Start Job Agent. Normally via start.bat (Windows), start.command (macOS, double-click) or ./start.sh
(macOS/Linux), which set up Python first and pass the same options:

    start                       your personal folder: <home>/JobAgent/default
    start --profile Alex        another person sharing this computer account: <home>/JobAgent/Alex
    start --home "/data/jobs"   keep your personal folder somewhere else
    start --no-browser          don't open a browser tab

Each person (each user account, or each --profile) gets their own data, port and access key, so one copy of
the source code serves everyone on the computer.
"""
import argparse
import hashlib
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="separate personal folder for another person (default: 'default')")
    ap.add_argument("--home", help="use this folder as the personal folder")
    ap.add_argument("--port", type=int, default=8765, help="preferred port (the next free one is used if taken)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()

    # app.config reads the personal folder and port from the environment, so reload it after setting them.
    from app import config
    try:
        home = config.home_for(args.profile, args.home)
    except ValueError as e:
        sys.exit(str(e))
    os.environ["JOB_AGENT_HOME"] = str(home)
    importlib.reload(config)
    home_id = hashlib.sha1(str(home).lower().encode()).hexdigest()[:16]
    config.ensure_home()
    key = config.session_key()

    # Already running for this person? Just open it. Otherwise take the first free port.
    candidates = []
    if config.PORT_FILE.exists():
        try:
            candidates.append(int(config.PORT_FILE.read_text().strip()))
        except ValueError:
            pass
    candidates += [p for p in range(args.port, args.port + 30) if p not in candidates]
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
