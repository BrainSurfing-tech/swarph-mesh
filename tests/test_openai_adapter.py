"""Tests for the OpenAI adapter — offline only, mocked AsyncOpenAI client.

Live smoke gated on ``OPENAI_API_KEY`` lives in ``test_smoke_openai.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarph_mesh.adapters.openai import (
    OpenAIAdapter,
    PRICING,
    _compute_cost,
    _to_openai_messages,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMAdapter


# ---------------------------------------------------------------------------
# Protocol fit
# ---------------------------------------------------------------------------


def test_adapter_satisfies_protocol():
    a = OpenAIAdapter(api_key="fake")
    assert isinstance(a, LLMAdapter)


def test_default_model_is_gpt_4o():
    a = OpenAIAdapter(api_key="fake")
    assert a.default_model == "gpt-4o"


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def test_compute_cost_gpt_4o():
    cost = _compute_cost("gpt-4o", 1_000_000, 1_000_000)
    assert cost == pytest.approx(2.50 + 10.00)


def test_compute_cost_gpt_4o_mini_cheaper_than_4o():
    mini = _compute_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    full = _compute_cost("gpt-4o", 1_000_000, 1_000_000)
    assert mini < full


def test_compute_cost_o1_premium():
    """o-series reasoner models are premium-priced."""
    o1 = _compute_cost("o1", 1_000_000, 1_000_000)
    full = _compute_cost("gpt-4o", 1_000_000, 1_000_000)
    assert o1 > full


def test_compute_cost_unknown_model_uses_default():
    cost_unknown = _compute_cost("gpt-future-2027", 1_000_000, 0)
    cost_default = _compute_cost("_default", 1_000_000, 0)
    assert cost_unknown == cost_default


def test_cost_per_token_returns_tuple():
    a = OpenAIAdapter(api_key="fake")
    inp, out = a.cost_per_token("gpt-4o")
    assert inp == 2.50
    assert out == 10.00


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def test_to_openai_messages_user():
    out = _to_openai_messages([ChatMessage(role="user", content="hi")])
    assert out == [{"role": "user", "content": "hi"}]


def test_to_openai_messages_multi_turn():
    msgs = [
        ChatMessage(role="user", content="q1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="q2"),
    ]
    out = _to_openai_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


# ---------------------------------------------------------------------------
# chat() — adapter wiring (mocked AsyncOpenAI client)
# ---------------------------------------------------------------------------


def _mock_response(
    *,
    text: str = "ok",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cached_tokens: int = 0,
    reasoning: Optional[str] = None,
    finish_reason: str = "stop",
):
    msg = MagicMock()
    msg.content = text
    msg.reasoning_content = reasoning  # None when not set

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason

    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


# Need Optional import for the helper signature. Backfill:
from typing import Optional  # noqa: E402


def test_chat_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = OpenAIAdapter()
    with pytest.raises(AdapterError, match="OPENAI_API_KEY"):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="gpt-4o",
            )
        )


def test_chat_extracts_usage_from_response():
    a = OpenAIAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_mock_response(
            text="response text",
            prompt_tokens=200,
            completion_tokens=100,
        )
    )
    a._client = mock_client

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="hello")],
            model="gpt-4o",
        )
    )
    assert resp.text == "response text"
    assert resp.input_tokens == 200
    assert resp.output_tokens == 100
    expected = (200 / 1_000_000) * 2.50 + (100 / 1_000_000) * 10.00
    assert resp.cost_usd == pytest.approx(expected)
    assert resp.duration_s >= 0
    assert resp.cached is False


def test_chat_marks_cached_when_cached_tokens_nonzero():
    a = OpenAIAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_mock_response(cached_tokens=80)
    )
    a._client = mock_client

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="x")],
            model="gpt-4o",
        )
    )
    assert resp.cached is True
    assert resp.raw_response["cached_tokens"] == 80


def test_chat_preserves_reasoning_as_preamble():
    """o-series models return reasoning_content separately. Adapter
    keeps it as preamble text wrapped in [reasoning]...[/reasoning]
    markers (consistent with DeepSeek + Claude adapters)."""
    a = OpenAIAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_mock_response(
            text="final answer",
            reasoning="step 1: think\nstep 2: conclude",
        )
    )
    a._client = mock_client

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="solve")],
            model="o1",
        )
    )
    assert "[reasoning]" in resp.text
    assert "step 1: think" in resp.text
    assert "[/reasoning]" in resp.text
    assert "final answer" in resp.text
    assert resp.raw_response["has_reasoning"] is True


def test_chat_prepends_system_prompt():
    a = OpenAIAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_mock_response())
    a._client = mock_client

    asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="gpt-4o",
            system_prompt="be terse",
        )
    )
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    msgs = call_kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1]["content"] == "hi"


def test_chat_passes_temperature_and_max_tokens():
    a = OpenAIAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_mock_response())
    a._client = mock_client

    asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="x")],
            model="gpt-4o",
            temperature=0.1,
            max_tokens=128,
        )
    )
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["temperature"] == 0.1
    assert call_kwargs["max_tokens"] == 128
    assert call_kwargs["stream"] is False


def test_chat_wraps_sdk_exceptions_as_adapter_error():
    a = OpenAIAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("API down")
    )
    a._client = mock_client

    with pytest.raises(AdapterError, match="OpenAIAdapter.chat failed"):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="gpt-4o",
            )
        )


def test_chat_handles_missing_usage_gracefully():
    """Some error paths return a response with usage=None; adapter
    should not crash."""
    a = OpenAIAdapter(api_key="fake")
    mock_client = MagicMock()
    response = MagicMock()
    msg = MagicMock()
    msg.content = "x"
    msg.reasoning_content = None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"
    response.choices = [choice]
    response.usage = None
    mock_client.chat.completions.create = AsyncMock(return_value=response)
    a._client = mock_client

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="x")],
            model="gpt-4o",
        )
    )
    assert resp.input_tokens == 0
    assert resp.output_tokens == 0
    assert resp.cost_usd == 0.0


# ---------------------------------------------------------------------------
# stream() — v0.5+ NotImplementedError
# ---------------------------------------------------------------------------


def test_stream_raises_not_implemented():
    a = OpenAIAdapter(api_key="fake")

    async def _consume():
        async for _ in a.stream(
            messages=[ChatMessage(role="user", content="x")],
            model="gpt-4o",
        ):
            pass

    with pytest.raises(NotImplementedError, match="v0.5"):
        asyncio.run(_consume())


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registry_dispatches_openai_provider():
    from swarph_mesh.adapters import get_adapter, reset_registry

    reset_registry()
    try:
        a1 = get_adapter("openai", api_key="fake")
        a2 = get_adapter("openai")
        assert isinstance(a1, OpenAIAdapter)
        assert a1 is a2  # singleton
    finally:
        reset_registry()
