"""Pipeline, gate and the two scaffold fixes (clauses 1, 3, 9, 11)."""

import inspect
import json

import pytest

from swarph_mesh.skunkworks import (
    GateRefusal,
    Pipeline,
    RunRefused,
    RunStatus,
    SkunkworksRequest,
    check_architect_verdict_gate,
)
from swarph_mesh.skunkworks.state import RunState
from skunk_fakes import ARCH, IDEA


def _req(**kw):
    base = dict(requesting_swarm="cell-a", persona="swarph", seed="seed", card_id=7)
    return SkunkworksRequest(**{**base, **kw})


def _arch(verdict, audit="audit text"):
    return lambda ctx: {"verdict": verdict, "mechanism": "m", "audit": audit}


def _pipe(tmp_path, arch=None, research=None, ideation=None, **kw):
    return Pipeline(
        runs_dir=str(tmp_path),
        ideation_runner=ideation or (lambda ctx: dict(IDEA)),
        research_runner=research
        or (lambda ctx: {"query": ctx["query"], "findings": "f"}),
        architect_runner=arch or _arch("APPROVED_FOR_BUILD"),
        **kw,
    )


# -- gate (ported from the source repo's test_gate, override removed) ----------


@pytest.mark.parametrize(
    "av,ok",
    [
        ("APPROVED_FOR_BUILD", True),
        ("NEEDS_REFINEMENT", False),
        ("REJECTED", False),
        (None, False),
        ("", False),
    ],
)
def test_gate_approved(av, ok):
    r = check_architect_verdict_gate(commander_verdict="APPROVED", arch_verdict=av)
    assert (r is None) is ok
    if not ok:
        assert (
            isinstance(r, GateRefusal)
            and r.to_dict()["error"] == "architect_verdict_gate"
        )
        assert r.arch_verdict == (av or "MISSING")


def test_gate_other_verdicts_always_pass_and_case_insensitive():
    for cv in ("REVISE", "REJECTED"):
        for av in ("APPROVED_FOR_BUILD", "NEEDS_REFINEMENT", None, ""):
            assert (
                check_architect_verdict_gate(commander_verdict=cv, arch_verdict=av)
                is None
            )
    assert (
        check_architect_verdict_gate(
            commander_verdict="approved", arch_verdict="approved_for_build"
        )
        is None
    )
    assert (
        check_architect_verdict_gate(
            commander_verdict="ApProVeD", arch_verdict="needs_refinement"
        )
        is not None
    )


def test_no_override_anywhere_in_the_library():
    """Clause 9: neither the gate nor approve takes an override, and nothing reads one."""
    assert "override" not in inspect.signature(check_architect_verdict_gate).parameters
    assert "override" not in inspect.signature(Pipeline.approve).parameters
    with pytest.raises(TypeError):
        Pipeline.approve(None, None, "APPROVED", override=True)  # type: ignore[call-arg]
    assert "arch_override_pre_authorized" not in SkunkworksRequest.model_fields


# -- pipeline ----------------------------------------------------------------


def test_run_reaches_awaiting_commander_and_persists_files(tmp_path):
    state = _pipe(tmp_path).run(_req())
    assert state.status == RunStatus.AWAITING_COMMANDER
    assert len(state.research) == 2
    d = tmp_path / state.run_id
    for name in ("state.json", "ideation.json", "research.json", "architecture.json"):
        assert (d / name).is_file(), name
    assert json.loads((d / "state.json").read_text())["status"] == "awaiting_commander"


def test_approve_gate_refusal_has_no_side_effects(tmp_path):
    p = _pipe(tmp_path, arch=_arch("NEEDS_REFINEMENT"))
    state = p.run(_req())
    res = p.approve(state, "APPROVED", notes="quote")
    assert (
        res["error"] == "architect_verdict_gate"
        and res["arch_verdict"] == "NEEDS_REFINEMENT"
    )
    assert state.status == RunStatus.AWAITING_COMMANDER
    assert not (tmp_path / state.run_id / "assessment.json").exists()


def test_approve_clean_writes_assessment_and_no_code_artifacts(tmp_path):
    p = _pipe(tmp_path)
    state = p.run(_req())
    res = p.approve(state, "approved", notes="the commander's words")
    assert res["status"] == "approved"
    a = json.loads((tmp_path / state.run_id / "assessment.json").read_text())
    assert (
        a["commander_words"] == "the commander's words" and a["audit"] == "audit text"
    )
    assert sorted(res["artifacts"]) == ["assessment.json"]


@pytest.mark.parametrize(
    "cv,status", [("REVISE", "revise_requested"), ("REJECTED", "rejected")]
)
def test_revise_and_reject_write_assessment(tmp_path, cv, status):
    p = _pipe(tmp_path, arch=_arch("REJECTED"))
    state = p.run(_req())
    assert p.approve(state, cv)["status"] == status
    assert (tmp_path / state.run_id / "assessment.json").is_file()


def test_approve_refuses_a_run_not_awaiting_commander(tmp_path):
    st = RunState.new("c", "swarph", "s")
    assert "not awaiting_commander" in _pipe(tmp_path).approve(st, "APPROVED")["error"]


# -- clause 11: the two scaffold fixes -----------------------------------------


def test_research_refusal_stops_the_run_not_swallowed(tmp_path):
    """Clause 11. Mutation: delete `except RunRefused: raise` -> the refusal becomes an
    {"error": ...} row and the run reaches AWAITING_COMMANDER (this test fails)."""

    def refusing(ctx):
        raise RunRefused("no web")

    state = _pipe(tmp_path, research=refusing).run(_req())
    assert state.status == RunStatus.ERROR and "no web" in state.error
    assert state.architecture is None


def test_other_research_errors_are_still_per_query_rows(tmp_path):
    def flaky(ctx):
        if ctx["query"] == "query A":
            raise ValueError("boom")
        return {"query": ctx["query"], "findings": "ok"}

    state = _pipe(tmp_path, research=flaky).run(_req())
    assert state.status == RunStatus.AWAITING_COMMANDER
    assert "error" in state.research[0] and state.research[1]["findings"] == "ok"


def test_architect_receives_this_runs_ideation_and_research(tmp_path):
    """Clause 11 canary. Mutation: drop `ideation=..., research=...` from _stage_architect's
    _build_context call -> ctx['ideation'] stays the None placeholder and this fails."""
    seen = {}

    def arch(ctx):
        seen.update(ctx)
        return dict(ARCH)

    idea = {**IDEA, "concept_name": "CANARY-IDEA"}
    p = _pipe(
        tmp_path,
        arch=arch,
        ideation=lambda c: dict(idea),
        research=lambda c: {"query": c["query"], "findings": "CANARY-FINDING"},
    )
    p.run(_req())
    assert seen["ideation"]["concept_name"] == "CANARY-IDEA"
    assert seen["research"][0]["findings"] == "CANARY-FINDING"


def test_queries_past_eight_are_cut_recorded_and_never_sent(tmp_path):
    sent = []
    idea = {**IDEA, "required_research_queries": [f"q{i}" for i in range(11)]}
    state = _pipe(
        tmp_path,
        ideation=lambda c: dict(idea),
        research=lambda c: sent.append(c["query"]) or {"query": c["query"]},
    ).run(_req())
    assert sent == [f"q{i}" for i in range(8)]
    assert state.cut_queries == ["q8", "q9", "q10"]


def test_request_refused_without_card_or_seed():
    with pytest.raises(ValueError):
        SkunkworksRequest(requesting_swarm="c", persona="swarph", seed="s")  # no card
    with pytest.raises(ValueError):
        SkunkworksRequest(requesting_swarm="c", persona="swarph", seed="  ", card_id=1)
    with pytest.raises(ValueError):
        SkunkworksRequest(requesting_swarm="c", persona="swarph", seed="s", card_id=0)
