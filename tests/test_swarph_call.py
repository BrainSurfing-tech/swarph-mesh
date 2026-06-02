"""Tests for the v0.1.0 SwarphCall public surface.

Most tests use a mock adapter so they run offline + cheap. The
falsifiability gate from PLAN.md §13 ('live smoke test against
real Gemini API returning text + writing attribution row') is
covered by ``tests/test_smoke_gemini.py`` and gated on
``GEMINI_API_KEY`` being set in the environment.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest

from swarph_mesh import (
    AttributionEvent,
    AttributionWriter,
    ChatMessage,
    HookSet,
    LLMResponse,
    SwarphCall,
    register_adapter,
)
from swarph_mesh.adapters import reset_registry
from swarph_mesh.attribution import (
    NullAttributionWriter,
    set_default_writer,
)
from swarph_mesh.hooks import attribution_post_call, default_hooks


# ---------------------------------------------------------------------------
# Mock adapter — minimal LLMAdapter Protocol fit for offline tests
# ---------------------------------------------------------------------------


class _MockAdapter:
    name = "mock"
    default_model = "mock-model-v1"

    def __init__(
        self,
        text: str = "ok",
        input_tokens: int = 10,
        output_tokens: int = 5,
        cost_usd: float = 0.001,
        raise_exc: Optional[BaseException] = None,
    ):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def chat(
        self,
        messages,
        model,
        system_prompt=None,
        json_schema=None,
        temperature=0.7,
        max_tokens=None,
    ):
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.raise_exc:
            raise self.raise_exc
        return LLMResponse(
            text=self.text,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            duration_s=0.05,
        )

    async def stream(self, messages, model, **kwargs) -> AsyncIterator[str]:
        if False:
            yield ""

    def cost_per_token(self, model: str) -> tuple[float, float]:
        return (0.075, 0.30)


@pytest.fixture
def mock_adapter():
    reset_registry()
    a = _MockAdapter()
    register_adapter("mock", a)
    yield a
    reset_registry()


@pytest.fixture
def file_writer(tmp_path):
    """Use a temp-dir attribution file so tests don't pollute
    ``~/.swarph/attribution.jsonl``."""
    from swarph_mesh.attribution import FileAttributionWriter

    w = FileAttributionWriter(path=tmp_path / "attribution.jsonl")
    set_default_writer(w)
    yield w
    set_default_writer(NullAttributionWriter())


# ---------------------------------------------------------------------------
# Caller-convention enforcement at construction
# ---------------------------------------------------------------------------


def test_swarph_call_validates_caller_at_construction():
    """Invalid caller raises BEFORE any adapter dispatch."""
    with pytest.raises(ValueError):
        SwarphCall(provider="mock", caller="invalidCaller")  # no dot, uppercase


def test_swarph_call_accepts_canonical_caller():
    SwarphCall(provider="mock", caller="orchestrator.boss")
    SwarphCall(provider="mock", caller="council.judge.claude.r2")


def test_swarph_call_rejects_non_dotted_caller():
    with pytest.raises(ValueError):
        SwarphCall(provider="mock", caller="flat_slug")


# ---------------------------------------------------------------------------
# Adapter dispatch
# ---------------------------------------------------------------------------


def test_chat_invokes_adapter_with_messages(mock_adapter, file_writer):
    sc = SwarphCall(provider="mock", caller="test.case.dispatch")
    asyncio.run(
        sc.chat(messages=[ChatMessage(role="user", content="hello")])
    )
    assert len(mock_adapter.calls) == 1
    assert mock_adapter.calls[0]["messages"][0].content == "hello"


def test_chat_returns_llm_response(mock_adapter, file_writer):
    sc = SwarphCall(provider="mock", caller="test.case.return")
    resp = asyncio.run(
        sc.chat(messages=[ChatMessage(role="user", content="hi")])
    )
    assert isinstance(resp, LLMResponse)
    assert resp.text == "ok"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    assert resp.cost_usd == pytest.approx(0.001)


def test_chat_uses_adapter_default_model_when_none(mock_adapter, file_writer):
    sc = SwarphCall(provider="mock", caller="test.case.default")
    asyncio.run(sc.chat(messages=[ChatMessage(role="user", content="x")]))
    assert mock_adapter.calls[0]["model"] == "mock-model-v1"


def test_chat_uses_explicit_model_override(mock_adapter, file_writer):
    sc = SwarphCall(
        provider="mock",
        caller="test.case.override",
        model="override-model-v2",
    )
    asyncio.run(sc.chat(messages=[ChatMessage(role="user", content="x")]))
    assert mock_adapter.calls[0]["model"] == "override-model-v2"


# ---------------------------------------------------------------------------
# Attribution hook — file-writer integration
# ---------------------------------------------------------------------------


def test_default_post_call_writes_attribution(mock_adapter, file_writer):
    sc = SwarphCall(provider="mock", caller="test.case.attribution", role="agents")
    asyncio.run(sc.chat(messages=[ChatMessage(role="user", content="hi")]))
    # Read the file and assert one row landed
    lines = file_writer.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["provider"] == "mock"
    assert row["caller"] == "test.case.attribution"
    assert row["role"] == "agents"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5
    assert row["cost_usd"] == pytest.approx(0.001)


def test_attribution_uses_per_call_writer_when_provided(mock_adapter, tmp_path):
    """Override the writer at SwarphCall construction time."""
    from swarph_mesh.attribution import FileAttributionWriter

    custom_path = tmp_path / "custom.jsonl"
    custom = FileAttributionWriter(path=custom_path)
    hooks = HookSet(post_call=[attribution_post_call(writer=custom)])

    sc = SwarphCall(
        provider="mock",
        caller="test.case.custom_writer",
        hooks=hooks,
    )
    asyncio.run(sc.chat(messages=[ChatMessage(role="user", content="x")]))
    assert custom_path.exists()
    assert len(custom_path.read_text().strip().splitlines()) == 1


def test_post_call_hook_failure_does_not_swallow_response(mock_adapter):
    """Hook errors must NOT lose the LLM response."""

    async def _bad_hook(ctx, resp):
        raise RuntimeError("hook is broken")

    hooks = HookSet(post_call=[_bad_hook])
    sc = SwarphCall(provider="mock", caller="test.case.hook_error", hooks=hooks)
    resp = asyncio.run(sc.chat(messages=[ChatMessage(role="user", content="x")]))
    assert resp.text == "ok"


# ---------------------------------------------------------------------------
# Pre-call hooks can mutate context
# ---------------------------------------------------------------------------


def test_pre_call_hook_can_rewrite_messages(mock_adapter, file_writer):
    """Redaction-layer pattern: pre_call hook mutates ``ctx.messages``."""

    async def _redactor(ctx):
        for m in ctx.messages:
            m.content = m.content.replace("SECRET", "[REDACTED]")

    hooks = HookSet(
        pre_call=[_redactor],
        post_call=[attribution_post_call()],
    )
    sc = SwarphCall(provider="mock", caller="test.case.redact", hooks=hooks)
    asyncio.run(
        sc.chat(messages=[ChatMessage(role="user", content="here is SECRET stuff")])
    )
    seen = mock_adapter.calls[0]["messages"][0].content
    assert "SECRET" not in seen
    assert "[REDACTED]" in seen


# ---------------------------------------------------------------------------
# on_error hook fires + original exception propagates
# ---------------------------------------------------------------------------


def test_on_error_hook_fires_then_reraises():
    reset_registry()
    bad_adapter = _MockAdapter(raise_exc=RuntimeError("boom"))
    register_adapter("mock_bad", bad_adapter)

    fired: list[BaseException] = []

    async def _on_err(ctx, exc):
        fired.append(exc)

    hooks = HookSet(on_error=[_on_err])
    sc = SwarphCall(provider="mock_bad", caller="test.case.on_error", hooks=hooks)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(sc.chat(messages=[ChatMessage(role="user", content="x")]))
    assert len(fired) == 1
    assert isinstance(fired[0], RuntimeError)
    reset_registry()


# ---------------------------------------------------------------------------
# JSON harness — parse-success + retry-once + retry-fail paths
# ---------------------------------------------------------------------------


def test_json_harness_parses_clean_response(mock_adapter, file_writer):
    """Adapter returns clean JSON → harness parses it on first try."""
    mock_adapter.text = '{"action": "BUY", "ticker": "MSFT"}'
    sc = SwarphCall(provider="mock", caller="test.json.clean")
    resp = asyncio.run(
        sc.chat(
            messages=[ChatMessage(role="user", content="give me a trade")],
            json_schema={"type": "object"},
        )
    )
    assert resp.parsed == {"action": "BUY", "ticker": "MSFT"}
    assert resp.error_class is None


def test_json_harness_retries_once_on_parse_fail(file_writer):
    """First call returns prose, retry call returns JSON."""
    reset_registry()

    class _RetryingAdapter:
        name = "retry"
        default_model = "retry-v1"

        def __init__(self):
            self.call_count = 0

        async def chat(self, messages, model, **kwargs):
            self.call_count += 1
            if self.call_count == 1:
                return LLMResponse(text="just prose, no json here", duration_s=0.01)
            return LLMResponse(text='{"ok": true}', duration_s=0.01)

        async def stream(self, *a, **kw):
            if False:
                yield ""

        def cost_per_token(self, model):
            return (0.0, 0.0)

    a = _RetryingAdapter()
    register_adapter("retry", a)

    sc = SwarphCall(provider="retry", caller="test.json.retry")
    resp = asyncio.run(
        sc.chat(
            messages=[ChatMessage(role="user", content="x")],
            json_schema={"type": "object"},
        )
    )
    assert a.call_count == 2  # initial + one retry
    assert resp.parsed == {"ok": True}
    assert resp.error_class is None
    reset_registry()


def test_json_harness_returns_malformed_when_retry_also_fails(file_writer):
    """Both attempts return prose → error_class='malformed_json'."""
    reset_registry()

    class _AlwaysProseAdapter:
        name = "prose"
        default_model = "prose-v1"

        async def chat(self, messages, model, **kwargs):
            return LLMResponse(text="words and more words", duration_s=0.01)

        async def stream(self, *a, **kw):
            if False:
                yield ""

        def cost_per_token(self, model):
            return (0.0, 0.0)

    register_adapter("prose", _AlwaysProseAdapter())

    sc = SwarphCall(provider="prose", caller="test.json.malformed")
    resp = asyncio.run(
        sc.chat(
            messages=[ChatMessage(role="user", content="x")],
            json_schema={"type": "object"},
        )
    )
    assert resp.parsed is None
    assert resp.error_class == "malformed_json"
    reset_registry()


# ---------------------------------------------------------------------------
# Unknown provider
# ---------------------------------------------------------------------------


def test_unknown_provider_raises_on_first_use():
    """Lazy resolution: SwarphCall construction succeeds for an
    unregistered provider, but first invocation raises."""
    sc = SwarphCall(provider="ghost", caller="test.case.ghost")
    with pytest.raises(Exception):  # UnknownProvider
        asyncio.run(sc.chat(messages=[ChatMessage(role="user", content="x")]))


def test_json_harness_retry_tokens_folded_into_resp(file_writer):
    """The retry consumes REAL tokens/cost; they must fold into resp so the
    single post_call attribution reflects TOTAL spend (adversarial-sweep MED —
    retry spend was invisible to attribution before)."""
    reset_registry()

    class _TokRetryAdapter:
        name = "tokretry"
        default_model = "t-v1"

        def __init__(self):
            self.n = 0

        async def chat(self, messages, model, **kwargs):
            self.n += 1
            if self.n == 1:
                return LLMResponse(text="prose, no json", input_tokens=100,
                                   output_tokens=20, cost_usd=0.05, duration_s=0.01)
            return LLMResponse(text='{"ok": true}', input_tokens=30,
                               output_tokens=10, cost_usd=0.02, duration_s=0.01)

        async def stream(self, *a, **kw):
            if False:
                yield ""

        def cost_per_token(self, model):
            return (0.0, 0.0)

    a = _TokRetryAdapter()
    register_adapter("tokretry", a)
    sc = SwarphCall(provider="tokretry", caller="test.tok.retry")
    resp = asyncio.run(sc.chat(
        messages=[ChatMessage(role="user", content="x")],
        json_schema={"type": "object"},
    ))
    assert a.n == 2
    assert resp.parsed == {"ok": True}
    # initial (100/20/$0.05) + retry (30/10/$0.02) folded into the one resp
    assert resp.input_tokens == 130
    assert resp.output_tokens == 30
    assert abs(resp.cost_usd - 0.07) < 1e-9
    reset_registry()
