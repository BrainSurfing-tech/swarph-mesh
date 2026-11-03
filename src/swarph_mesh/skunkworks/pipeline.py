"""4-node skeleton: ideation, research, architect, commander gate.

Runners are injected, so this module has no model dependency at import. The stage
contexts are built here and are the ONLY thing a runner sees (stage isolation):

* ideation   : the persona's ideation section, the seed, the blocker question
* research   : one query
* architect  : the persona's architect section, THIS run's ideation and research,
               and the --context-file contents (these reach no other stage)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .artifacts import persist, write_assessment_always
from .ceilings import MAX_RESEARCH_QUERIES, RunRefused
from .gate import check_architect_verdict_gate
from .persona import PersonaPack, load_persona
from .request import SkunkworksRequest
from .state import RunState, RunStatus

logger = logging.getLogger(__name__)

PipelineNode = Callable[[Dict[str, Any]], Dict[str, Any]]
"""A node takes its stage context dict and returns its structured output."""


@dataclass
class Pipeline:
    runs_dir: str
    ideation_runner: Optional[PipelineNode] = None
    research_runner: Optional[PipelineNode] = (
        None  # called once per query, sequentially
    )
    architect_runner: Optional[PipelineNode] = None
    persona_dir: Optional[str] = None
    on_stage: Callable[[RunState, str], None] = lambda s, stage: None

    def run(
        self, request: SkunkworksRequest, state: Optional[RunState] = None
    ) -> RunState:
        state = state or RunState.new(
            request.requesting_swarm, request.persona, request.seed, request.card_id
        )
        try:
            persona = load_persona(request.persona, base_dir=self.persona_dir)
            persist(state, self.runs_dir)
            self._stage_ideation(state, request, persona)
            self._stage_research(state, request, persona)
            self._stage_architect(state, request, persona)
            state.transition(RunStatus.AWAITING_COMMANDER)
            persist(state, self.runs_dir)
            self.on_stage(state, "awaiting_commander")
        except Exception as exc:
            logger.exception(
                "Skunkworks run %s failed at %s", state.run_id, state.status.value
            )
            state.error = repr(exc)
            state.transition(RunStatus.ERROR)
            persist(state, self.runs_dir)
            self.on_stage(state, "error")
        return state

    def approve(
        self, state: RunState, commander_verdict: str, notes: str = ""
    ) -> Dict[str, Any]:
        """Commander decision: APPROVED / REJECTED / REVISE. There is no override."""
        if state.status != RunStatus.AWAITING_COMMANDER:
            return {
                "error": f"run {state.run_id} is in status {state.status.value!r}, not awaiting_commander"
            }
        cv = (commander_verdict or "").strip().upper()
        if cv not in {"APPROVED", "REJECTED", "REVISE"}:
            return {
                "error": f"verdict must be APPROVED / REJECTED / REVISE, got {commander_verdict!r}"
            }

        arch_verdict = ((state.architecture or {}).get("verdict") or "").strip().upper()
        refusal = check_architect_verdict_gate(
            commander_verdict=cv, arch_verdict=arch_verdict
        )
        if refusal is not None:
            return refusal.to_dict()

        state.commander_verdict = cv
        state.commander_notes = notes or ""
        state.arch_verdict_at_decision = arch_verdict
        state.transition(
            {"APPROVED": RunStatus.APPROVED, "REJECTED": RunStatus.REJECTED}.get(
                cv, RunStatus.REVISE_REQUESTED
            )
        )
        persist(state, self.runs_dir)
        assessment = write_assessment_always(state, self.runs_dir)
        return {
            "run_id": state.run_id,
            "status": state.status.value,
            "arch_verdict_at_decision": arch_verdict,
            "artifacts": {"assessment.json": assessment},
        }

    # -- stages ------------------------------------------------------------

    def _need(self, runner: Optional[PipelineNode], name: str) -> PipelineNode:
        if runner is None:
            raise RunRefused(f"no {name} runner configured")
        return runner

    def _stage_ideation(
        self, state: RunState, request: SkunkworksRequest, persona: PersonaPack
    ) -> None:
        state.transition(RunStatus.IDEATING)
        persist(state, self.runs_dir)
        state.ideation = self._need(self.ideation_runner, "ideation")(
            self._build_context(request, persona, stage="ideation")
        )
        persist(state, self.runs_dir)
        self.on_stage(state, "ideation")

    def _stage_research(
        self, state: RunState, request: SkunkworksRequest, persona: PersonaPack
    ) -> None:
        state.transition(RunStatus.RESEARCHING)
        persist(state, self.runs_dir)
        raw = (state.ideation or {}).get("required_research_queries", []) or []
        queries: List[str] = [q for q in raw if isinstance(q, str) and q.strip()]
        state.cut_queries = queries[MAX_RESEARCH_QUERIES:]  # recorded, never sent
        queries = queries[:MAX_RESEARCH_QUERIES]
        runner = self._need(self.research_runner, "research")
        results: List[Dict[str, Any]] = []
        for i, q in enumerate(queries, start=1):
            ctx = self._build_context(request, persona, stage="research", query=q)
            try:
                results.append(runner(ctx))
            except RunRefused:
                raise  # a refusal stops the run; it is not a per-query miss
            except Exception as e:
                logger.warning("research query %d failed: %s", i, e)
                results.append({"query": q, "error": repr(e)})
        state.research = results
        persist(state, self.runs_dir)
        self.on_stage(state, "research")

    def _stage_architect(
        self, state: RunState, request: SkunkworksRequest, persona: PersonaPack
    ) -> None:
        state.transition(RunStatus.ARCHITECTING)
        persist(state, self.runs_dir)
        ctx = self._build_context(
            request,
            persona,
            stage="architect",
            ideation=state.ideation,
            research=state.research,
        )
        state.architecture = self._need(self.architect_runner, "architect")(ctx)
        persist(state, self.runs_dir)
        self.on_stage(state, "architecture")

    def _build_context(
        self, request: SkunkworksRequest, persona: PersonaPack, **extra
    ) -> Dict[str, Any]:
        stage = extra["stage"]
        if stage == "ideation":
            ctx: Dict[str, Any] = {
                "system": persona.ideation_system,
                "seed": request.seed,
                "blocker_question": request.blocker_question,
            }
        elif stage == "research":
            ctx = {}
        else:
            ctx = {
                "system": persona.architect_system,
                "ideation": None,
                "research": None,
                "swarm_context": _read_context_files(request.context_md_paths),
            }
        return {**ctx, **extra}  # extra overrides the None placeholders above


def _read_context_files(paths: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                out[p] = f.read()
        except Exception as e:
            out[p] = f"(failed to read: {e})"
    return out
