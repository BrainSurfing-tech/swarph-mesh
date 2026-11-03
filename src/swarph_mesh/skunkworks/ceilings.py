"""Every ceiling in one module: session window, job/time/day caps, stop file, run lock.

A failed check raises RunRefused, which the pipeline turns into a run in ERROR.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, time as dtime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

MAX_JOBS_PER_RUN = 10  # ideation 1 + research up to MAX_RESEARCH_QUERIES + architect 1
MAX_RESEARCH_QUERIES = 8
MAX_RUN_SECONDS = 1800
MAX_RUNS_PER_DAY = 3  # per cell per UTC day, ERROR runs included


@lru_cache(maxsize=1)
def _ny() -> ZoneInfo:
    # Resolved on first use, not at import: a host without tz data (Windows without the
    # tzdata package) still imports this module, and fails with a clear error when asked.
    return ZoneInfo("America/New_York")


SESSION_OPEN = dtime(9, 30)
SESSION_CLOSE = dtime(16, 0)


class RunRefused(Exception):
    """A ceiling, a refusal from a runner or a wrong-lane answer: the run stops in ERROR."""


def session_blocked(now: Optional[datetime] = None) -> bool:
    """True Mon-Fri while New York local time is in [09:30, 16:00).

    The shared job queue serves live judges first-in-first-out, so runs stay out of the
    US cash session. No holiday calendar: a market holiday is refused too.
    A naive ``now`` is read as UTC.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ny = now.astimezone(_ny())
    return ny.weekday() < 5 and SESSION_OPEN <= ny.time() < SESSION_CLOSE


def state_root() -> Path:
    raw = os.environ.get("SWARPH_STATE") or os.environ.get("SWARPH_STATE_ROOT")
    return Path(raw) if raw else Path.home() / "swarph_state"


def runs_dir_for(cell: str, root: Optional[Path] = None) -> Path:
    return (root or state_root()) / cell / "skunkworks"


class JobGuard:
    """Called before every model job (queue or research), in this order: session window,
    stop file, job count, wall clock. Passing counts the job."""

    def __init__(
        self,
        run_dir: str,
        *,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_count: Callable[[int], None] = lambda n: None,
    ):
        self.run_dir = run_dir
        self.clock = clock
        self.now = now
        self.on_count = on_count
        self.started = clock()
        self.jobs_used = 0

    def remaining_seconds(self) -> float:
        return MAX_RUN_SECONDS - (self.clock() - self.started)

    def check(self) -> None:
        if session_blocked(self.now()):
            raise RunRefused(
                "refused: US cash session (Mon-Fri 09:30-16:00 America/New_York)"
            )
        if os.path.exists(os.path.join(self.run_dir, "STOP")):
            raise RunRefused("refused: STOP file present")
        if self.jobs_used >= MAX_JOBS_PER_RUN:
            raise RunRefused(f"refused: job ceiling {MAX_JOBS_PER_RUN} reached")
        if self.remaining_seconds() <= 0:
            raise RunRefused(f"refused: wall clock ceiling {MAX_RUN_SECONDS}s reached")
        self.jobs_used += 1
        self.on_count(self.jobs_used)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":  # os.kill(pid, 0) would TERMINATE the process there
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(
            0x1000, False, pid
        )  # QUERY_LIMITED_INFORMATION
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
        return bool(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runs_today(runs_dir: Path, now: Optional[datetime] = None) -> int:
    """Run dirs created on today's UTC date (parsed from sk_<epoch>_<hex>), ERROR included."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    n = 0
    if runs_dir.is_dir():
        for p in runs_dir.iterdir():
            parts = p.name.split("_")
            if (
                p.is_dir()
                and len(parts) == 3
                and parts[0] == "sk"
                and parts[1].isdigit()
            ):
                if datetime.fromtimestamp(int(parts[1]), timezone.utc).date() == today:
                    n += 1
    return n


class RunLock:
    """One live run per cell: ``<runs_dir>/.lock`` holds the owner's pid. A lock whose pid
    is dead is taken over."""

    def __init__(self, runs_dir: Path):
        self.path = runs_dir / ".lock"

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid_alive(pid):
                    raise RunRefused(f"refused: another run is live (pid {pid})")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return
        raise RunRefused("refused: could not take the run lock")

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
