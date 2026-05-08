"""Live OpenAI smoke — Phase 4 #4 falsifiability gate per PLAN.md §13.

Skipped unless ``OPENAI_API_KEY`` is set. Single small call against
gpt-4o-mini (cheapest tier) — verifies wire compatibility, auth,
response shape, usage extraction, and cost computation against real
infrastructure.

Run from a fresh venv with the published wheel to validate the
release candidate:

    pip install --no-cache-dir swarph-mesh==0.5.0 openai
    OPENAI_API_KEY=sk-... pytest tests/test_smoke_openai.py -v
"""

from __future__ import annotations

import asyncio
import os

import pytest

from swarph_mesh.adapters.openai import OpenAIAdapter
from swarph_mesh.types import ChatMessage


pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping live smoke",
)


def test_smoke_openai_gpt_4o_mini_chat():
    """Single small call against gpt-4o-mini. Verifies wire format,
    auth, response extraction, usage reporting, cost computation."""
    adapter = OpenAIAdapter()
    resp = asyncio.run(
        adapter.chat(
            messages=[
                ChatMessage(role="user", content="Say PONG and nothing else.")
            ],
            model="gpt-4o-mini",
            temperature=0.0,
            max_tokens=8,
        )
    )
    assert resp.text  # non-empty
    assert resp.input_tokens > 0
    assert resp.output_tokens > 0
    assert resp.cost_usd > 0
    assert resp.cost_usd < 0.01  # tiny call should be sub-cent
    assert resp.duration_s > 0
    print(
        f"\nopenai smoke: text={resp.text!r} "
        f"in={resp.input_tokens} out={resp.output_tokens} "
        f"cost=${resp.cost_usd:.6f} dur={resp.duration_s:.2f}s"
    )
