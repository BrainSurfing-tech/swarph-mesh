"""Live smoke test for the DeepSeek adapter — Phase 4 falsifiability gate
per PLAN.md §13:

    SwarphCall(provider="deepseek", caller="cli.smoke").chat([
        ChatMessage(role="user", content="hi")
    ])
    → returns text + writes attribution row

Gated on ``DEEPSEEK_API_KEY`` being set in the environment OR the
legacy ``/home/ubuntu/deepseek/.env`` file existing. Skipped on hosts
without either source.

Real API costs ~$0.0001 per call on V4-Flash + a 5-token prompt.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from swarph_mesh import ChatMessage, SwarphCall
from swarph_mesh.adapters.deepseek import _resolve_api_key
from swarph_mesh.attribution import FileAttributionWriter, set_default_writer
from swarph_mesh.hooks import default_hooks


pytestmark = pytest.mark.skipif(
    _resolve_api_key() is None,
    reason="DEEPSEEK_API_KEY not set + no /home/ubuntu/deepseek/.env; live smoke skipped",
)


def test_phase_4_deepseek_falsifiability_gate(tmp_path):
    """End-to-end: SwarphCall → DeepSeekAdapter → real API → LLMResponse
    → attribution row.

    Failures here mean Phase 4 doesn't pass its gate. Don't ship
    v0.3.0 with this test failing.
    """
    attribution_path = tmp_path / "smoke_attribution.jsonl"
    writer = FileAttributionWriter(path=attribution_path)
    set_default_writer(writer)

    sc = SwarphCall(
        provider="deepseek",
        caller="cli.smoke.phase_4_deepseek_gate",
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

    # 3. Cost computed (V4-Flash is cheap but >0 for any nonzero tokens)
    assert resp.cost_usd > 0
    assert resp.cost_usd < 0.01  # sanity — should be tiny

    # 4. Latency measured
    assert resp.duration_s > 0

    # 5. Attribution row written
    assert attribution_path.exists()
    lines = attribution_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["provider"] == "deepseek"
    assert row["caller"] == "cli.smoke.phase_4_deepseek_gate"
    assert row["role"] == "agents"
    assert row["input_tokens"] == resp.input_tokens
    assert row["output_tokens"] == resp.output_tokens
    assert row["cost_usd"] == pytest.approx(resp.cost_usd)
