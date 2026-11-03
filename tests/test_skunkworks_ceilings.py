"""Clause 6 and 7: the session window and every ceiling, one test each."""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from swarph_mesh.skunkworks import (
    RunRefused,
    RunStatus,
    SkunkworksRequest,
    session_blocked,
)
from swarph_mesh.skunkworks import ceilings
from swarph_mesh.skunkworks.artifacts import persist
from swarph_mesh.skunkworks.ceilings import JobGuard, runs_today
from swarph_mesh.skunkworks.service import default_runners, start_run, sweep_dead_runs
from swarph_mesh.skunkworks.state import RunState
from skunk_fakes import SATURDAY, FakeClaude, FakeGateway, live_get


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


# EST: Tue 2026-01-13 (UTC-5), window is 14:30Z-21:00Z.  EDT: Tue 2026-07-14 (UTC-4), 13:30Z-20:00Z.
@pytest.mark.parametrize(
    "when,blocked",
    [
        (utc(2026, 1, 13, 14, 29, 59), False),
        (utc(2026, 1, 13, 14, 30), True),
        (utc(2026, 1, 13, 20, 59, 59), True),
        (utc(2026, 1, 13, 21, 0), False),
        (utc(2026, 1, 13, 13, 30), False),  # 13:30Z is inside the window only in EDT
        (utc(2026, 7, 14, 13, 29, 59), False),
        (utc(2026, 7, 14, 13, 30), True),
        (utc(2026, 7, 14, 19, 59, 59), True),
        (utc(2026, 7, 14, 20, 0), False),
        (utc(2026, 7, 14, 20, 30), False),  # 20:30Z is inside the window only in EST
        (utc(2026, 11, 7, 15, 0), False),
        (utc(2026, 11, 8, 15, 0), False),  # Sat, Sun
        (utc(2026, 11, 6, 15, 0), True),  # Fri
    ],
)
def test_session_window_est_edt_weekend_and_boundaries(when, blocked):
    assert session_blocked(when) is blocked


def test_naive_now_is_read_as_utc_and_default_now_works():
    assert session_blocked(datetime(2026, 1, 13, 15, 0)) is True
    assert session_blocked() in (True, False)


def _guard(tmp_path, **kw):
    kw.setdefault("now", lambda: SATURDAY)
    return JobGuard(str(tmp_path), **kw)


def test_eleventh_job_is_refused_before_it_is_sent(tmp_path):
    gw = FakeGateway()
    from swarph_mesh.skunkworks.runners import QueueRunner, IDEATION_SCHEMA

    g = _guard(tmp_path)
    jobs = []
    q = QueueRunner(
        stage="ideation",
        model="gemini-4-argon-high",
        want_lane="agy",
        schema=IDEATION_SCHEMA,
        gateway_url="http://gw",
        cell="c",
        run_id="r",
        guard=g,
        post=gw.post,
        get=gw.get,
        jobs=jobs,
        sleep=lambda s: None,
    )
    for _ in range(10):
        q({"system": "s", "seed": "x", "blocker_question": None})
    assert gw.posts == 10 and g.jobs_used == 10
    with pytest.raises(RunRefused, match="job ceiling"):
        q({"system": "s", "seed": "x", "blocker_question": None})
    assert gw.posts == 10  # the 11th never reached the queue


def test_wall_clock_ceiling(tmp_path):
    t = [1000.0]
    g = _guard(tmp_path, clock=lambda: t[0])
    g.check()
    t[0] += ceilings.MAX_RUN_SECONDS - 1
    g.check()
    t[0] += 2
    with pytest.raises(RunRefused, match="wall clock"):
        g.check()
    assert ceilings.MAX_RUN_SECONDS == 1800


def test_stop_file_refuses_the_next_job(tmp_path):
    g = _guard(tmp_path)
    g.check()
    (tmp_path / "STOP").write_text("")
    with pytest.raises(RunRefused, match="STOP"):
        g.check()


def test_check_order_is_session_stop_count_clock(tmp_path):
    t = [0.0]
    g = JobGuard(str(tmp_path), clock=lambda: t[0], now=lambda: utc(2026, 1, 13, 15, 0))
    (tmp_path / "STOP").write_text("")
    g.jobs_used = ceilings.MAX_JOBS_PER_RUN
    t[0] = 10**6
    with pytest.raises(RunRefused, match="cash session"):
        g.check()
    g.now = lambda: SATURDAY
    with pytest.raises(RunRefused, match="STOP"):
        g.check()
    (tmp_path / "STOP").unlink()
    with pytest.raises(RunRefused, match="job ceiling"):
        g.check()
    g.jobs_used = 0
    with pytest.raises(RunRefused, match="wall clock"):
        g.check()


def _fake_factory(tmp_path, gw=None, claude=None):
    gw = gw or FakeGateway()
    return gw, default_runners(
        gateway_url="http://gw",
        cell="cell-a",
        post=gw.post,
        get=gw.get,
        claude_bin="/bin/claude",
        research_run=claude or FakeClaude(),
        url_get=live_get,
    )


def _req(**kw):
    base = dict(requesting_swarm="cell-a", persona="swarph", seed="seed", card_id=1)
    return SkunkworksRequest(**{**base, **kw})


def test_start_is_refused_in_the_session_window_and_creates_nothing(tmp_path):
    gw, fac = _fake_factory(tmp_path)
    with pytest.raises(RunRefused, match="cash session"):
        start_run(
            _req(),
            runners_factory=fac,
            state_root=tmp_path,
            now=lambda: utc(2026, 1, 13, 15, 0),
        )
    assert not any(tmp_path.rglob("sk_*")) and gw.posts == 0


def test_a_full_faked_run_uses_four_jobs_one_at_a_time(tmp_path):
    gw, fac = _fake_factory(tmp_path)
    st = start_run(
        _req(), runners_factory=fac, state_root=tmp_path, now=lambda: SATURDAY
    )
    assert st.status == RunStatus.AWAITING_COMMANDER
    assert st.jobs_used == 4  # ideation + 2 research + architect
    assert gw.posts == 2  # the queue saw ideation and architect only
    assert not (tmp_path / "cell-a" / "skunkworks" / ".lock").exists()  # released


def test_three_runs_per_cell_per_utc_day_error_runs_counted(tmp_path):
    rdir = tmp_path / "cell-a" / "skunkworks"
    for i in range(3):
        (rdir / f"sk_{int(SATURDAY.timestamp()) - i}_{i:08x}").mkdir(parents=True)
    # a run from yesterday does not count
    (rdir / f"sk_{int((SATURDAY - timedelta(days=1)).timestamp())}_deadbeef").mkdir()
    assert runs_today(rdir, SATURDAY) == 3
    gw, fac = _fake_factory(tmp_path)
    with pytest.raises(RunRefused, match="3 runs"):
        start_run(
            _req(), runners_factory=fac, state_root=tmp_path, now=lambda: SATURDAY
        )
    assert gw.posts == 0
    # next UTC day the cap is open again
    nxt = lambda: (
        SATURDAY + timedelta(days=2)
    )  # Monday 12:00Z is in the NY window? 07:00 NY: no
    assert (
        start_run(_req(), runners_factory=fac, state_root=tmp_path, now=nxt).status
        == RunStatus.AWAITING_COMMANDER
    )


def test_one_live_run_per_cell(tmp_path):
    rdir = tmp_path / "cell-a" / "skunkworks"
    rdir.mkdir(parents=True)
    (rdir / ".lock").write_text(str(os.getpid()))  # this process is alive
    gw, fac = _fake_factory(tmp_path)
    with pytest.raises(RunRefused, match="another run is live"):
        start_run(
            _req(), runners_factory=fac, state_root=tmp_path, now=lambda: SATURDAY
        )
    assert gw.posts == 0


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_dead_pid_lock_is_taken_over_and_its_run_marked_error(tmp_path):
    rdir = tmp_path / "cell-a" / "skunkworks"
    pid = _dead_pid()
    old = RunState.new("cell-a", "swarph", "s", 1)
    old.pid = pid
    old.transition(RunStatus.RESEARCHING)
    (rdir).mkdir(parents=True)
    persist(old, str(rdir))
    (rdir / ".lock").write_text(str(pid))
    gw, fac = _fake_factory(tmp_path)
    st = start_run(
        _req(), runners_factory=fac, state_root=tmp_path, now=lambda: SATURDAY
    )
    assert st.status == RunStatus.AWAITING_COMMANDER
    import json

    prev = json.loads((rdir / old.run_id / "state.json").read_text())
    assert prev["status"] == "error" and "died" in prev["error"]


def test_sweep_leaves_a_live_run_and_finished_runs_alone(tmp_path):
    live = RunState.new("c", "swarph", "s", 1)
    live.transition(RunStatus.IDEATING)  # pid = this process
    done = RunState.new("c", "swarph", "s", 1)
    done.pid = _dead_pid()
    done.transition(RunStatus.AWAITING_COMMANDER)
    persist(live, str(tmp_path))
    persist(done, str(tmp_path))
    assert sweep_dead_runs(tmp_path) == []


def test_nothing_resumes_a_run_and_no_daemon_or_queue_file_exists():
    """Clause 7: the package has no resume path, thread or persisted queue."""
    import ast, swarph_mesh.skunkworks as pkg

    banned = {"resume", "daemon", "Thread", "fork", "Popen", "sched"}
    for fn in os.listdir(os.path.dirname(pkg.__file__)):
        if fn.endswith(".py"):
            tree = ast.parse(
                open(os.path.join(os.path.dirname(pkg.__file__), fn)).read()
            )
            names = (
                {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                | {
                    n.name
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                }
            )
            assert not (names & banned), (fn, names & banned)


def test_stale_ceiling_constants():
    assert (
        ceilings.MAX_JOBS_PER_RUN,
        ceilings.MAX_RESEARCH_QUERIES,
        ceilings.MAX_RUNS_PER_DAY,
    ) == (10, 8, 3)


def test_unknown_persona_is_refused_before_any_run_dir_or_daily_slot(tmp_path):
    gw, fac = _fake_factory(tmp_path)
    with pytest.raises(FileNotFoundError):
        start_run(
            _req(persona="no-such-persona"),
            runners_factory=fac,
            state_root=tmp_path,
            now=lambda: SATURDAY,
        )
    assert not list((tmp_path / "cell-a" / "skunkworks").glob("sk_*"))
    assert gw.posts == 0
