"""Saved searches on a schedule.

While Job Agent runs, the Scheduler checks every minute for saved searches whose time has come and runs them one at a
time, never while another search is running. When Job Agent is closed, `run.py --run-searches` does the same once and
exits, for your OS scheduler (Task Scheduler, launchd/cron) to call; `run.py --schedule-help` prints the command.
"""
import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import HOME, ROOT
from .search import SearchRunner

CHECK_EVERY = 60  # seconds between looks at the schedule
JITTER = 30       # up to this many seconds' random delay, so runs don't all fire on the minute


def next_run_at(s: dict):
    """When a saved search runs next on its own (None: only when you click Run)."""
    if not s.get("enabled") or not s.get("every_hours"):
        return None
    if not s.get("last_run_at"):
        return datetime.now().isoformat(timespec="seconds")
    return (datetime.fromisoformat(s["last_run_at"]) + timedelta(hours=s["every_hours"])).isoformat(timespec="seconds")


def due_searches(now: datetime | None = None) -> list:
    """Enabled saved searches with a schedule whose next run time has come, most overdue first."""
    now = now or datetime.now()
    due = []
    for s in db.saved_searches():
        nxt = next_run_at(s)
        if nxt and datetime.fromisoformat(nxt) <= now:
            due.append((nxt, s))
    return [s for _, s in sorted(due, key=lambda x: x[0])]


class Scheduler:
    def __init__(self, runner: SearchRunner):
        self.runner = runner
        self.task = None

    def start(self):
        self.task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self):
        if self.task:
            self.task.cancel()

    async def _loop(self):
        await asyncio.sleep(5)  # let the server finish starting
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one failed run stop the schedule
                print(f"Job Agent: scheduled search failed: {e!r}", flush=True)
            await asyncio.sleep(CHECK_EVERY)

    async def tick(self, jitter=JITTER) -> bool:
        """Run the most overdue saved search, if any and if no search is running. Returns whether one ran."""
        if self.runner.state["running"]:
            return False
        due = await asyncio.to_thread(due_searches)
        if not due:
            return False
        if jitter:
            await asyncio.sleep(random.uniform(0, jitter))
            if self.runner.state["running"]:  # you started one meanwhile
                return False
        self.runner.start(False, spec=due[0], trigger="schedule")
        await self.runner.task
        return True


async def run_due_once(trigger="headless") -> list:
    """Run every due saved search, one after another (Job Agent closed: called by run.py --run-searches)."""
    results = []
    for s in due_searches():
        runner = SearchRunner()
        runner.start(False, spec=s, trigger=trigger)
        await runner.task
        results.append({"name": s["name"], **{k: runner.state[k] for k in ("new_jobs", "refreshed", "alerts", "errors")}})
    return results


def schedule_help() -> dict:
    """The command that runs due saved searches while Job Agent is closed, and how to register it on this computer."""
    py = Path(sys.executable)
    if sys.platform == "win32" and (py.parent / "pythonw.exe").exists():
        py = py.parent / "pythonw.exe"  # no console window popping up every hour
    command = f'"{py}" "{ROOT / "run.py"}" --run-searches --home "{HOME}"'
    if sys.platform == "win32":
        tr = command.replace('"', '\\"')
        register = f'schtasks /Create /TN "Job Agent searches" /SC HOURLY /F /TR "{tr}"'
        how = ("Run this once in a Command Prompt to have Windows Task Scheduler check your saved searches every hour "
               "(remove it with: schtasks /Delete /TN \"Job Agent searches\" /F).")
    else:
        register = f"(crontab -l 2>/dev/null; echo '7 * * * * {command} >/dev/null 2>&1') | crontab -"
        how = ("Run this once in a terminal to have cron check your saved searches every hour "
               "(remove it with: crontab -e and delete the line).")
    return {"command": command, "register": register, "how": how,
            "note": "Each saved search still runs only as often as its own schedule says. While Job Agent is open "
                    "it runs them itself, so the scheduled command then does nothing."}
