"""Tests for the Gemini adapter — offline only, mocked SDK.

Live smoke test against real Gemini API lives in
``test_smoke_gemini.py`` (gated on GEMINI_API_KEY env var).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from swarph_mesh.adapters.gemini import (
    GeminiAdapter,
    PRICING,
    _compute_cost,
    _to_langchain_messages,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMAdapter


# ---------------------------------------------------------------------------
# Protocol fit
# ---------------------------------------------------------------------------


def test_adapter_satisfies_protocol():
    a = GeminiAdapter(api_key="fake-for-protocol-check")
    assert isinstance(a, LLMAdapter)


def test_default_model_is_flash():
    a = GeminiAdapter(api_key="fake")
    assert a.default_model == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def test_compute_cost_known_model():
    # 1M input + 1M output on flash @ ($0.075, $0.30) base
    cost = _compute_cost("gemini-2.5-flash", 1_000_000, 1_000_000, flex=False)
    assert cost == pytest.approx(0.075 + 0.30)


def test_compute_cost_flex_50pct_rebate():
    base = _compute_cost("gemini-2.5-flash", 1_000_000, 1_000_000, flex=False)
    flex = _compute_cost("gemini-2.5-flash", 1_000_000, 1_000_000, flex=True)
    assert flex == pytest.approx(base * 0.5)


def test_compute_cost_unknown_model_uses_default():
    cost_unknown = _compute_cost("gemini-future-model-2027", 1_000_000, 0, flex=False)
    cost_default = _compute_cost("_default", 1_000_000, 0, flex=False)
    assert cost_unknown == cost_default


def test_cost_per_token_returns_tuple():
    a = GeminiAdapter(api_key="fake")
    inp, out = a.cost_per_token("gemini-2.5-flash")
    assert inp == 0.075
    assert out == 0.30


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def test_to_langchain_messages_user():
    out = _to_langchain_messages([ChatMessage(role="user", content="hi")])
    from langchain_core.messages import HumanMessage

    assert len(out) == 1
    assert isinstance(out[0], HumanMessage)
    assert out[0].content == "hi"


def test_to_langchain_messages_assistant():
    out = _to_langchain_messages([ChatMessage(role="assistant", content="ok")])
    from langchain_core.messages import AIMessage

    assert isinstance(out[0], AIMessage)


def test_to_langchain_messages_system():
    out = _to_langchain_messages([ChatMessage(role="system", content="rules")])
    from langchain_core.messages import SystemMessage

    assert isinstance(out[0], SystemMessage)


def test_to_langchain_messages_unknown_role_falls_through():
    """Unknown roles wrap into HumanMessage with prefix so the call
    doesn't fail; bridge surfaces unknown-role errors itself."""
    out = _to_langchain_messages([ChatMessage(role="weird", content="x")])
    assert "[weird]" in out[0].content


# ---------------------------------------------------------------------------
# chat() — adapter wiring (mocked bridge)
# ---------------------------------------------------------------------------


def test_chat_requires_api_key():
    """Without GEMINI_API_KEY env or kwarg, chat raises AdapterError."""
    import os

    with patch.dict(os.environ, {}, clear=True):
        a = GeminiAdapter()
        with pytest.raises(AdapterError, match="GEMINI_API_KEY"):
            asyncio.run(
                a.chat(
                    messages=[ChatMessage(role="user", content="x")],
                    model="gemini-2.5-flash",
                )
            )


def test_chat_extracts_usage_from_ai_message():
    """Mock the bridge: usage_metadata propagates through to LLMResponse."""
    from langchain_core.messages import AIMessage

    mock_bridge = MagicMock()
    mock_bridge.invoke.return_value = AIMessage(
        content="response text",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_token_details": {"cache_read": 0},
        },
    )

    a = GeminiAdapter(api_key="fake-test-key", flex=True)
    a._bridges[("gemini-2.5-flash", True)] = mock_bridge

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="hello")],
            model="gemini-2.5-flash",
        )
    )
    assert resp.text == "response text"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    # Flex tier on flash: (100/1M * 0.075) + (50/1M * 0.30) = 0.0000225 → /2 = 0.0000113
    expected_cost = ((100 / 1_000_000) * 0.075 + (50 / 1_000_000) * 0.30) * 0.5
    assert resp.cost_usd == pytest.approx(expected_cost)
    assert resp.duration_s >= 0


def test_chat_marks_cached_when_cache_read_nonzero():
    from langchain_core.messages import AIMessage

    mock_bridge = MagicMock()
    mock_bridge.invoke.return_value = AIMessage(
        content="cached!",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_token_details": {"cache_read": 80},
        },
    )

    a = GeminiAdapter(api_key="fake", flex=False)
    a._bridges[("gemini-2.5-pro", False)] = mock_bridge

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="x")],
            model="gemini-2.5-pro",
        )
    )
    assert resp.cached is True
    assert resp.raw_response["cached_tokens"] == 80


def test_chat_wraps_bridge_exceptions_as_adapter_error():
    mock_bridge = MagicMock()
    mock_bridge.invoke.side_effect = RuntimeError("bridge crashed")

    a = GeminiAdapter(api_key="fake", flex=True)
    a._bridges[("gemini-2.5-flash", True)] = mock_bridge

    with pytest.raises(AdapterError, match="GeminiAdapter.chat failed"):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="gemini-2.5-flash",
            )
        )


# ---------------------------------------------------------------------------
# stream() — v0.1.0 raises NotImplementedError
# ---------------------------------------------------------------------------


def test_stream_raises_not_implemented():
    a = GeminiAdapter(api_key="fake")

    async def _consume():
        async for _ in a.stream(
            messages=[ChatMessage(role="user", content="x")],
            model="gemini-2.5-flash",
        ):
            pass

    with pytest.raises(NotImplementedError, match="v0.2.0"):
        asyncio.run(_consume())
