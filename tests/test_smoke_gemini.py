"""Live smoke test against real Gemini API — falsifiability gate
for Phase 1 per PLAN.md §13:

  Live smoke test: SwarphCall(provider="gemini", caller="cli.smoke")
  .chat(["hi"]) returns text + writes attribution row

Gated on ``GEMINI_API_KEY`` being set in the environment. Skipped
on CI without the key. Costs ~$0.0001 per run on Flash + Flex tier.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from swarph_mesh import (
    ChatMessage,
    SwarphCall,
)
from swarph_mesh.attribution import FileAttributionWriter, set_default_writer
from swarph_mesh.hooks import default_hooks


pytestmark = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set; live smoke test skipped",
)


def test_phase_1_falsifiability_gate(tmp_path):
    """End-to-end: SwarphCall → GeminiAdapter → bridge → real API
    → LLMResponse → attribution row.

    Failures here mean Phase 1 doesn't pass its gate. Don't ship
    v0.1.0 with this test failing.
    """
    # Use a temp attribution file so the smoke test doesn't pollute
    # the developer's ~/.swarph/attribution.jsonl.
    attribution_path = tmp_path / "smoke_attribution.jsonl"
    writer = FileAttributionWriter(path=attribution_path)
    set_default_writer(writer)

    sc = SwarphCall(
        provider="gemini",
        caller="cli.smoke.phase_1_gate",
        role="agents",
        hooks=default_hooks(writer=writer),
    )
    resp = asyncio.run(
        sc.chat(
            messages=[ChatMessage(role="user", content="say 'pong' and nothing else")],
        )
    )

    # 1. Returns text
    assert resp.text  # non-empty response
    assert isinstance(resp.text, str)

    # 2. Tokens populated
    assert resp.input_tokens > 0
    assert resp.output_tokens > 0

    # 3. Cost computed (Flex tier rebate applied: should be small but >0)
    assert resp.cost_usd > 0
    assert resp.cost_usd < 0.01  # sanity — should be cheap

    # 4. Latency measured
    assert resp.duration_s > 0

    # 5. Attribution row written
    assert attribution_path.exists()
    lines = attribution_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["provider"] == "gemini"
    assert row["caller"] == "cli.smoke.phase_1_gate"
    assert row["role"] == "agents"
    assert row["input_tokens"] == resp.input_tokens
    assert row["output_tokens"] == resp.output_tokens
    assert row["cost_usd"] == pytest.approx(resp.cost_usd)
