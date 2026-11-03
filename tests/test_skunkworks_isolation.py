"""Clause 3: stage isolation, proven on every prompt captured in a full faked run."""

import json

import pytest

from swarph_mesh.skunkworks import RunStatus, SkunkworksRequest
from swarph_mesh.skunkworks.runners import RESEARCH_INSTRUCTIONS
from swarph_mesh.skunkworks.service import default_runners, start_run
from skunk_fakes import IDEA, SATURDAY, FakeClaude, FakeGateway, live_get

OLD, CTX, IDEA_SYS, ARCH_SYS = (
    "CANARY-OLD-RUN",
    "CANARY-CONTEXT-FILE",
    "CANARY-IDEATION-SECTION",
    "CANARY-ARCHITECT-SECTION",
)
SEED, BLOCK, IDEA_OUT, RES_OUT = (
    "CANARY-SEED",
    "CANARY-BLOCKER",
    "CANARY-IDEATION-OUTPUT",
    "CANARY-RESEARCH-OUTPUT",
)


@pytest.fixture
def captured(tmp_path):
    # an earlier run's directory, full of a canary
    old = tmp_path / "state" / "cell-a" / "skunkworks" / "sk_1_00000000"
    old.mkdir(parents=True)
    for n in ("request", "state", "ideation", "research", "architecture", "assessment"):
        (old / f"{n}.json").write_text(json.dumps({"x": OLD}))
    # a persona whose two sections each carry a canary
    pdir = tmp_path / "personas"
    pdir.mkdir()
    (pdir / "swarph.md").write_text(
        f"# IDEATION_SYSTEM\n{IDEA_SYS}\n\n# ARCHITECT_SYSTEM\n{ARCH_SYS}\n"
    )
    ctx = tmp_path / "ctx.md"
    ctx.write_text(CTX)

    gw = FakeGateway(ideation={**IDEA, "concept_name": IDEA_OUT})
    claude = FakeClaude(
        json.dumps({"findings": RES_OUT, "urls": ["https://example.org/a"]})
    )
    fac = default_runners(
        gateway_url="http://gw",
        cell="cell-a",
        post=gw.post,
        get=gw.get,
        claude_bin="/x/claude",
        research_run=claude,
        url_get=live_get,
    )
    req = SkunkworksRequest(
        requesting_swarm="cell-a",
        persona="swarph",
        seed=SEED,
        card_id=1,
        blocker_question=BLOCK,
        context_md_paths=[str(ctx)],
    )
    st = start_run(
        req,
        runners_factory=fac,
        state_root=tmp_path / "state",
        persona_dir=str(pdir),
        now=lambda: SATURDAY,
    )
    assert st.status == RunStatus.AWAITING_COMMANDER, st.error
    by = {b["jobs"][0]["purpose"].split("-")[1]: b["jobs"][0] for b in gw.bodies}
    return {
        "ideation": by["ideation"]["system_prompt"] + "\n" + by["ideation"]["prompt"],
        "architect": by["architect"]["system_prompt"]
        + "\n"
        + by["architect"]["prompt"],
        "research": [c["argv"][2] for c in claude.calls],
    }


def test_ideation_holds_persona_seed_and_blocker_only(captured):
    p = captured["ideation"]
    assert IDEA_SYS in p and SEED in p and BLOCK in p
    for canary in (ARCH_SYS, CTX, OLD, IDEA_OUT, RES_OUT):
        assert canary not in p, canary


def test_research_holds_one_query_and_fixed_instructions(captured):
    assert len(captured["research"]) == 2
    for q, p in zip(("query A", "query B"), captured["research"]):
        assert p == RESEARCH_INSTRUCTIONS + "\n\n" + q
        for canary in (SEED, BLOCK, IDEA_SYS, ARCH_SYS, CTX, OLD, IDEA_OUT):
            assert canary not in p, canary


def test_architect_holds_this_runs_ideation_research_and_context_file(captured):
    """Clause 3 mutation: use persona.architect_system for ideation (or the reverse) and the
    ideation test fails; drop the ideation/research kwargs and this one fails (clause 11)."""
    p = captured["architect"]
    assert ARCH_SYS in p and IDEA_OUT in p and RES_OUT in p and CTX in p
    for canary in (IDEA_SYS, OLD, BLOCK):
        assert canary not in p, canary


def test_context_file_reaches_the_architect_only(captured):
    assert CTX in captured["architect"]
    assert CTX not in captured["ideation"] and all(
        CTX not in r for r in captured["research"]
    )


def test_an_earlier_run_reaches_no_stage(captured):
    assert OLD not in captured["ideation"] + captured["architect"] + "".join(
        captured["research"]
    )
