"""Live Grok smoke — Phase 4 #5 falsifiability gate per PLAN.md §13.

Skipped unless ``XAI_API_KEY`` (or ``GROK_API_KEY`` alias) is set.
Single small call against grok-3-mini (cheapest tier) — verifies
wire compatibility against api.x.ai, auth, response shape, usage
extraction, and cost computation against real infrastructure.

Run from a fresh venv with the published wheel to validate the
release candidate:

    pip install --no-cache-dir swarph-mesh==0.5.0 openai
    XAI_API_KEY=xai-... pytest tests/test_smoke_grok.py -v
"""

from __future__ import annotations

import asyncio
import os

import pytest

from swarph_mesh.adapters.grok import GrokAdapter
from swarph_mesh.types import ChatMessage


pytestmark = pytest.mark.skipif(
    not (os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")),
    reason="XAI_API_KEY/GROK_API_KEY not set — skipping live smoke",
)


def test_smoke_grok_3_mini_chat():
    """Single small call against grok-3-mini. Verifies wire format,
    base_url=api.x.ai, auth, response extraction, usage reporting,
    cost computation."""
    adapter = GrokAdapter()
    resp = asyncio.run(
        adapter.chat(
            messages=[
                ChatMessage(role="user", content="Say PONG and nothing else.")
            ],
            model="grok-3-mini",
            temperature=0.0,
            max_tokens=8,
        )
    )
    assert resp.text
    assert resp.input_tokens > 0
    assert resp.output_tokens > 0
    assert resp.cost_usd > 0
    assert resp.cost_usd < 0.01
    assert resp.duration_s > 0
    print(
        f"\ngrok smoke: text={resp.text!r} "
        f"in={resp.input_tokens} out={resp.output_tokens} "
        f"cost=${resp.cost_usd:.6f} dur={resp.duration_s:.2f}s"
    )
