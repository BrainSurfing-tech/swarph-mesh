"""Live smoke test for the Claude subscription adapter — Phase 4 #3
falsifiability gate per PLAN.md §13:

    SwarphCall(provider="claude", caller="cli.smoke").chat([
        ChatMessage(role="user", content="hi")
    ])
    → returns text via subscription billing (claude -p path)
    → writes attribution row with cost_usd=0 (subscription is flat-rate)

Gated on:
- ``~/.claude/.credentials.json`` existing + readable
- ``claude`` binary on PATH (via ``CLAUDE_BIN`` env or standard install)
- ``swarph_shared.verify_subscription_setup`` passing

Skipped on hosts without subscription auth.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from swarph_mesh import ChatMessage, SwarphCall
from swarph_mesh.adapters.claude import _resolve_claude_bin
from swarph_mesh.attribution import FileAttributionWriter, set_default_writer
from swarph_mesh.hooks import default_hooks


def _can_run_smoke() -> bool:
    """Mirror of swarph_shared.verify_subscription_setup logic without
    raising — used for pytest.mark.skipif."""
    creds = Path.home() / ".claude" / ".credentials.json"
    if not creds.exists():
        return False
    if not Path(_resolve_claude_bin()).exists():
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _can_run_smoke(),
    reason="Claude subscription smoke needs ~/.claude/.credentials.json + claude binary",
)


def test_phase_4_claude_falsifiability_gate(tmp_path):
    """End-to-end: SwarphCall → ClaudeAdapter → claude -p (subscription)
    → JSON output → LLMResponse → attribution row.

    Failures here mean Phase 4 #3 doesn't pass its gate. Don't ship
    v0.4.0 with this test failing.
    """
    attribution_path = tmp_path / "smoke_attribution.jsonl"
    writer = FileAttributionWriter(path=attribution_path)
    set_default_writer(writer)

    sc = SwarphCall(
        provider="claude",
        caller="cli.smoke.phase_4_claude_gate",
        role="agents",
        hooks=default_hooks(writer=writer),
    )
    resp = asyncio.run(
        sc.chat(
            messages=[
                ChatMessage(role="user", content="say 'pong' and nothing else")
            ],
        )
    )

    # 1. Returns text
    assert resp.text  # non-empty
    assert isinstance(resp.text, str)

    # 2. Tokens populated (--output-format=json gives us real counts)
    assert resp.input_tokens > 0
    assert resp.output_tokens > 0

    # 3. Subscription path → cost_usd is ALWAYS 0.0 (flat-rate, not metered)
    assert resp.cost_usd == 0.0

    # 4. Metered-equivalent cost preserved in raw_response for auditors
    assert resp.raw_response["billing_path"] == "subscription"
    assert "api_metered_cost_usd" in resp.raw_response
    assert resp.raw_response["api_metered_cost_usd"] >= 0

    # 5. Latency measured
    assert resp.duration_s > 0

    # 6. Attribution row written with cost_usd=0 (honest subscription
    # report)
    assert attribution_path.exists()
    lines = attribution_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["provider"] == "claude"
    assert row["caller"] == "cli.smoke.phase_4_claude_gate"
    assert row["role"] == "agents"
    assert row["cost_usd"] == 0.0  # subscription path is flat-rate
    assert row["input_tokens"] == resp.input_tokens
    assert row["output_tokens"] == resp.output_tokens
