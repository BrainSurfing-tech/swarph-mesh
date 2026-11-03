"""Start a run in the calling process: session window, dead-run sweep, per-day cap,
one-live-run lock, then the pipeline. No daemon, no persisted queue, no resume."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .artifacts import persist, read_json, run_dir, write_json
from .ceilings import (
    MAX_RUNS_PER_DAY,
    JobGuard,
    RunLock,
    RunRefused,
    pid_alive,
    runs_dir_for,
    runs_today,
    session_blocked,
)
from .persona import load_persona
from .pipeline import Pipeline, PipelineNode
from .request import SkunkworksRequest
from .runners import (
    ARCHITECT_LANE,
    ARCHITECT_MODEL,
    ARCHITECT_SCHEMA,
    IDEATION_LANE,
    IDEATION_MODEL,
    IDEATION_SCHEMA,
    Get,
    Post,
    QueueRunner,
    ResearchRunner,
)
from .state import RUNNING_STATUSES, RunState, RunStatus

RUN_ID_RE = re.compile(r"^sk_\d+_[0-9a-f]{8}$")

# (run_id, guard, jobs) -> (ideation, research, architect) runners
RunnersFactory = Callable[
    [str, JobGuard, List[Dict[str, Any]]],
    Tuple[PipelineNode, PipelineNode, PipelineNode],
]


def default_runners(
    *,
    gateway_url: str,
    cell: str,
    post: Post,
    get: Get,
    claude_bin: Optional[str] = None,
    research_run: Optional[Callable[..., Any]] = None,
    url_get: Optional[Callable[..., Any]] = None,
    url_resolve: Optional[Callable[[str], list]] = None,
) -> RunnersFactory:
    def make(run_id: str, guard: JobGuard, jobs: List[Dict[str, Any]]):
        common: Dict[str, Any] = dict(
            gateway_url=gateway_url,
            cell=cell,
            run_id=run_id,
            guard=guard,
            post=post,
            get=get,
            jobs=jobs,
        )
        return (
            QueueRunner(
                stage="ideation",
                model=IDEATION_MODEL,
                want_lane=IDEATION_LANE,
                schema=IDEATION_SCHEMA,
                **common,
            ),
            ResearchRunner(
                guard=guard,
                claude_bin=claude_bin,
                get=url_get,
                resolve=url_resolve,
                **({"run": research_run} if research_run else {}),
            ),
            QueueRunner(
                stage="architect",
                model=ARCHITECT_MODEL,
                want_lane=ARCHITECT_LANE,
                schema=ARCHITECT_SCHEMA,
                **common,
            ),
        )

    return make


def sweep_dead_runs(runs_dir: Path) -> List[str]:
    """A state file left RUNNING whose pid is dead becomes ERROR. Nothing resumes it."""
    fixed: List[str] = []
    if not runs_dir.is_dir():
        return fixed
    for d in sorted(runs_dir.iterdir()):
        raw = read_json(str(d / "state.json")) if d.is_dir() else None
        if not isinstance(raw, dict):
            continue
        try:
            st = RunState.from_dict(raw)
        except (TypeError, ValueError):
            continue  # not a run state we wrote; leave it alone
        if st.status in RUNNING_STATUSES and not pid_alive(st.pid):
            st.error = (
                f"process {st.pid} died while {st.status.value}; nothing resumes a run"
            )
            st.transition(RunStatus.ERROR)
            persist(st, str(runs_dir))
            fixed.append(st.run_id)
    return fixed


def start_run(
    request: SkunkworksRequest,
    *,
    runners_factory: RunnersFactory,
    state_root: Optional[Path] = None,
    persona_dir: Optional[str] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    clock: Callable[[], float] = time.time,
    on_stage: Callable[[RunState, str], None] = lambda s, stage: None,
) -> RunState:
    cell = request.requesting_swarm
    load_persona(
        request.persona, base_dir=persona_dir
    )  # a typo'd persona costs no daily slot
    if session_blocked(now()):
        raise RunRefused(
            "refused: US cash session (Mon-Fri 09:30-16:00 America/New_York)"
        )
    rdir = runs_dir_for(cell, state_root)
    rdir.mkdir(parents=True, exist_ok=True)
    lock = RunLock(rdir)
    lock.acquire()  # a live run holds it; a dead pid's lock is taken over
    try:
        sweep_dead_runs(rdir)
        if runs_today(rdir, now()) >= MAX_RUNS_PER_DAY:
            raise RunRefused(
                f"refused: {MAX_RUNS_PER_DAY} runs already started today (UTC)"
            )
        state = RunState.new(cell, request.persona, request.seed, request.card_id)
        os.makedirs(run_dir(str(rdir), state.run_id))
        write_json(
            os.path.join(run_dir(str(rdir), state.run_id), "request.json"),
            request.model_dump(),
        )

        def count(n: int) -> None:
            state.jobs_used = n

        guard = JobGuard(
            run_dir(str(rdir), state.run_id), clock=clock, now=now, on_count=count
        )
        ideation, research, architect = runners_factory(state.run_id, guard, state.jobs)
        pipeline = Pipeline(
            runs_dir=str(rdir),
            ideation_runner=ideation,
            research_runner=research,
            architect_runner=architect,
            persona_dir=persona_dir,
            on_stage=on_stage,
        )
        return pipeline.run(request, state)
    finally:
        lock.release()
