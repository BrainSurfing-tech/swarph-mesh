"""Tests for the Grok adapter — offline only, mocked AsyncOpenAI client.

Live smoke gated on ``XAI_API_KEY`` (or ``GROK_API_KEY`` alias) lives
in ``test_smoke_grok.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarph_mesh.adapters.grok import (
    GrokAdapter,
    PRICING,
    XAI_BASE_URL,
    _compute_cost,
    _resolve_api_key,
    _to_openai_messages,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMAdapter


# ---------------------------------------------------------------------------
# Protocol fit + construction
# ---------------------------------------------------------------------------


def test_adapter_satisfies_protocol():
    a = GrokAdapter(api_key="fake")
    assert isinstance(a, LLMAdapter)


def test_default_model_is_grok_4():
    a = GrokAdapter(api_key="fake")
    assert a.default_model == "grok-4"


def test_default_base_url_is_xai_endpoint():
    a = GrokAdapter(api_key="fake")
    assert a._base_url == XAI_BASE_URL == "https://api.x.ai/v1"


def test_base_url_override():
    a = GrokAdapter(api_key="fake", base_url="http://custom-proxy:9999")
    assert a._base_url == "http://custom-proxy:9999"


# ---------------------------------------------------------------------------
# API-key resolution — XAI_API_KEY wins, GROK_API_KEY is alias
# ---------------------------------------------------------------------------


def test_resolve_api_key_prefers_xai_api_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-canonical")
    monkeypatch.setenv("GROK_API_KEY", "grok-alias")
    assert _resolve_api_key() == "xai-canonical"


def test_resolve_api_key_falls_back_to_grok_api_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GROK_API_KEY", "grok-only")
    assert _resolve_api_key() == "grok-only"


def test_resolve_api_key_returns_none_when_neither_set(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    assert _resolve_api_key() is None


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def test_compute_cost_grok_4():
    cost = _compute_cost("grok-4", 1_000_000, 1_000_000)
    assert cost == pytest.approx(5.00 + 15.00)


def test_compute_cost_grok_3_mini_cheaper_than_grok_4():
    mini = _compute_cost("grok-3-mini", 1_000_000, 1_000_000)
    full = _compute_cost("grok-4", 1_000_000, 1_000_000)
    assert mini < full


def test_compute_cost_grok_2_alias_routes_to_grok_3_pricing():
    """xAI's alias-routing — grok-2 calls land on grok-3 backend at
    grok-3 pricing."""
    g2 = _compute_cost("grok-2", 1_000_000, 1_000_000)
    g3 = _compute_cost("grok-3", 1_000_000, 1_000_000)
    assert g2 == g3


def test_compute_cost_unknown_model_uses_default():
    cost_unknown = _compute_cost("grok-future-2027", 1_000_000, 0)
    cost_default = _compute_cost("_default", 1_000_000, 0)
    assert cost_unknown == cost_default


def test_cost_per_token_returns_tuple():
    a = GrokAdapter(api_key="fake")
    inp, out = a.cost_per_token("grok-4")
    assert inp == 5.00
    assert out == 15.00


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
    ]
    out = _to_openai_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# chat() — adapter wiring (mocked AsyncOpenAI client against api.x.ai)
# ---------------------------------------------------------------------------


def _mock_response(
    *,
    text: str = "ok",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cached_tokens: int = 0,
    reasoning: Optional[str] = None,
):
    msg = MagicMock()
    msg.content = text
    msg.reasoning_content = reasoning

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"

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


def test_chat_requires_api_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    a = GrokAdapter()
    with pytest.raises(AdapterError, match="XAI_API_KEY"):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="grok-4",
            )
        )


def test_chat_extracts_usage_from_response():
    a = GrokAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_mock_response(
            text="grok response",
            prompt_tokens=300,
            completion_tokens=100,
        )
    )
    a._client = mock_client

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="hello")],
            model="grok-4",
        )
    )
    assert resp.text == "grok response"
    assert resp.input_tokens == 300
    assert resp.output_tokens == 100
    expected = (300 / 1_000_000) * 5.00 + (100 / 1_000_000) * 15.00
    assert resp.cost_usd == pytest.approx(expected)


def test_chat_marks_cached_when_cached_tokens_nonzero():
    a = GrokAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_mock_response(cached_tokens=120)
    )
    a._client = mock_client

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="x")],
            model="grok-4",
        )
    )
    assert resp.cached is True
    assert resp.raw_response["cached_tokens"] == 120


def test_chat_preserves_reasoning_as_preamble():
    """Grok reasoner variants return reasoning_content separately;
    adapter wraps in [reasoning] markers consistent with the rest of
    the adapter family."""
    a = GrokAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=_mock_response(
            text="final",
            reasoning="my chain of thought",
        )
    )
    a._client = mock_client

    resp = asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="solve")],
            model="grok-4",
        )
    )
    assert "[reasoning]" in resp.text
    assert "my chain of thought" in resp.text
    assert "[/reasoning]" in resp.text
    assert "final" in resp.text


def test_chat_prepends_system_prompt():
    a = GrokAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=_mock_response())
    a._client = mock_client

    asyncio.run(
        a.chat(
            messages=[ChatMessage(role="user", content="hi")],
            model="grok-4",
            system_prompt="be helpful",
        )
    )
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    msgs = call_kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "be helpful"}


def test_chat_wraps_sdk_exceptions_as_adapter_error():
    a = GrokAdapter(api_key="fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(
        side_effect=RuntimeError("xAI down")
    )
    a._client = mock_client

    with pytest.raises(AdapterError, match="GrokAdapter.chat failed"):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="grok-4",
            )
        )


# ---------------------------------------------------------------------------
# stream() — v0.5+ NotImplementedError
# ---------------------------------------------------------------------------


def test_stream_raises_not_implemented():
    a = GrokAdapter(api_key="fake")

    async def _consume():
        async for _ in a.stream(
            messages=[ChatMessage(role="user", content="x")],
            model="grok-4",
        ):
            pass

    with pytest.raises(NotImplementedError, match="v0.5"):
        asyncio.run(_consume())


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registry_dispatches_grok_provider():
    from swarph_mesh.adapters import get_adapter, reset_registry

    reset_registry()
    try:
        a1 = get_adapter("grok", api_key="fake")
        a2 = get_adapter("grok")
        assert isinstance(a1, GrokAdapter)
        assert a1 is a2  # singleton
    finally:
        reset_registry()


def test_unknown_provider_error_lists_phase_4_complete():
    """v0.5.0 error message: all five adapters shipped."""
    from swarph_mesh.adapters import get_adapter, reset_registry
    from swarph_mesh.exceptions import UnknownProvider

    reset_registry()
    with pytest.raises(UnknownProvider, match="all five Phase 4 adapters"):
        get_adapter("nonexistent-provider")
