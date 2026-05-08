"""Tests for the Claude subscription adapter — offline only, mocked subprocess.

Live smoke gated on ~/.claude/.credentials.json + claude binary
existing lives in ``test_smoke_claude.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from swarph_mesh.adapters.claude import (
    DEFAULT_MODEL,
    ClaudeAdapter,
    PRICING,
    _build_prompt,
    _parse_claude_json,
    _resolve_claude_bin,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMAdapter


# ---------------------------------------------------------------------------
# Protocol fit
# ---------------------------------------------------------------------------


def test_adapter_satisfies_protocol():
    a = ClaudeAdapter(claude_bin="/fake/path")
    assert isinstance(a, LLMAdapter)


def test_default_model():
    a = ClaudeAdapter(claude_bin="/fake")
    assert a.default_model == DEFAULT_MODEL == "claude-opus-4-7"


def test_api_key_kwarg_is_no_op_with_warning(caplog):
    """Passing an api_key logs a warning but does not error — the
    LLMAdapter Protocol shape includes it, but subscription path
    uses creds file, not env keys."""
    import logging

    with caplog.at_level(logging.WARNING):
        ClaudeAdapter(api_key="sk-ant-fake-test-key", claude_bin="/fake")
    assert any("ignores api_key" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# claude_bin resolution
# ---------------------------------------------------------------------------


def test_claude_bin_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/custom/path/claude")
    assert _resolve_claude_bin() == "/custom/path/claude"


def test_claude_bin_falls_back_to_home_local_bin(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)
    fake_bin = fake_home / ".local" / "bin" / "claude"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    with patch.object(Path, "home", return_value=fake_home):
        assert _resolve_claude_bin() == str(fake_bin)


# ---------------------------------------------------------------------------
# Cost calculation (metered-equivalent for subscription auditors)
# ---------------------------------------------------------------------------


def test_pricing_table_has_canonical_models():
    """Lock the current Anthropic line so an accidental rename in the
    PRICING dict gets caught at CI."""
    assert "claude-opus-4-7" in PRICING
    assert "claude-sonnet-4-6" in PRICING
    assert "claude-haiku-4-5" in PRICING
    # opus is more expensive than sonnet which is more than haiku
    assert PRICING["claude-opus-4-7"][1] > PRICING["claude-sonnet-4-6"][1]
    assert PRICING["claude-sonnet-4-6"][1] > PRICING["claude-haiku-4-5"][1]


def test_cost_per_token_returns_tuple():
    a = ClaudeAdapter(claude_bin="/fake")
    inp, out = a.cost_per_token("claude-opus-4-7")
    assert inp == 5.00
    assert out == 25.00


def test_cost_per_token_unknown_model_uses_default():
    a = ClaudeAdapter(claude_bin="/fake")
    inp, out = a.cost_per_token("claude-future-2027")
    assert (inp, out) == PRICING["_default"]


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def test_build_prompt_single_user_turn():
    out = _build_prompt(
        [ChatMessage(role="user", content="hi")], system_prompt=None
    )
    assert "[USER]" in out
    assert "hi" in out


def test_build_prompt_with_system():
    out = _build_prompt(
        [ChatMessage(role="user", content="hi")],
        system_prompt="be terse",
    )
    # System prompt comes first, no role marker
    assert out.startswith("be terse")
    assert "[USER]" in out


def test_build_prompt_multi_turn():
    out = _build_prompt(
        [
            ChatMessage(role="user", content="q1"),
            ChatMessage(role="assistant", content="a1"),
            ChatMessage(role="user", content="q2"),
        ],
        system_prompt=None,
    )
    # All three turns appear in order with role headers
    assert out.find("[USER]\nq1") < out.find("[ASSISTANT]\na1")
    assert out.find("[ASSISTANT]\na1") < out.find("[USER]\nq2")


# ---------------------------------------------------------------------------
# JSON output parser
# ---------------------------------------------------------------------------


def test_parse_claude_json_happy_path():
    payload = json.dumps({"result": "hi", "is_error": False, "usage": {}})
    out = _parse_claude_json(payload)
    assert out["result"] == "hi"


def test_parse_claude_json_empty_raises():
    with pytest.raises(AdapterError, match="empty response"):
        _parse_claude_json("")


def test_parse_claude_json_malformed_raises():
    with pytest.raises(AdapterError, match="failed to parse"):
        _parse_claude_json("{not valid json")


# ---------------------------------------------------------------------------
# Subscription verification
# ---------------------------------------------------------------------------


def test_verify_idempotent():
    """Calling verify() multiple times only runs the underlying check
    once — subsequent calls are no-ops."""
    a = ClaudeAdapter(claude_bin="/fake")
    with patch(
        "swarph_mesh.adapters.claude.verify_subscription_setup"
    ) as mock_verify:
        a.verify()
        a.verify()
        a.verify()
    assert mock_verify.call_count == 1


def test_verify_propagates_subscription_setup_failure():
    a = ClaudeAdapter(claude_bin="/fake")
    with patch(
        "swarph_mesh.adapters.claude.verify_subscription_setup",
        side_effect=RuntimeError("creds missing"),
    ):
        with pytest.raises(RuntimeError, match="creds missing"):
            a.verify()


# ---------------------------------------------------------------------------
# chat() — subprocess invocation (mocked)
# ---------------------------------------------------------------------------


def _mock_subprocess_response(
    *,
    text: str = "ok",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_creation: int = 0,
    is_error: bool = False,
    api_error_status: object = None,
    total_cost_usd: float = 0.001,
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    """Build a MagicMock with the claude -p --output-format=json shape."""
    payload = {
        "result": text,
        "is_error": is_error,
        "api_error_status": api_error_status,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
        "total_cost_usd": total_cost_usd,
        "duration_api_ms": 500,
        "session_id": "sid-123",
        "stop_reason": "end_turn",
    }
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = json.dumps(payload)
    proc.stderr = stderr
    return proc


def test_chat_invokes_claude_with_correct_argv():
    """Verify the subprocess gets called with -p, --model, --output-format json."""
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True  # skip verify
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return _mock_subprocess_response(text="hello")

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="hi")],
                model="claude-opus-4-7",
            )
        )
    argv = captured["argv"]
    assert argv[0] == "/fake/claude"
    assert "-p" in argv
    assert "--model" in argv
    assert "claude-opus-4-7" in argv
    assert "--output-format" in argv
    assert "json" in argv


def test_chat_strips_anthropic_api_key_from_subprocess_env(monkeypatch):
    """Critical billing-leak prevention: ANTHROPIC_API_KEY must NEVER
    appear in subprocess env. swarph_shared.scrub_env_for_subprocess
    handles this; verify the adapter actually calls it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-leak-test-key")
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _mock_subprocess_response()

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="claude-opus-4-7",
            )
        )
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    # IS_SANDBOX gets injected (per OMEGA opus_subscription convention)
    assert captured["env"].get("IS_SANDBOX") == "1"
    # PATH/HOME survive (claude needs them)
    assert "PATH" in captured["env"]


def test_chat_passes_json_schema_to_cli():
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _mock_subprocess_response(text='{"action": "BUY"}')

    schema = {"type": "object", "required": ["action"]}
    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="give a trade")],
                model="claude-opus-4-7",
                json_schema=schema,
            )
        )
    assert "--json-schema" in captured["argv"]
    schema_idx = captured["argv"].index("--json-schema")
    assert json.loads(captured["argv"][schema_idx + 1]) == schema


def test_chat_extracts_usage_from_response():
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True
    with patch(
        "subprocess.run",
        return_value=_mock_subprocess_response(
            text="response text",
            input_tokens=200,
            output_tokens=100,
        ),
    ):
        resp = asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="hello")],
                model="claude-opus-4-7",
            )
        )
    assert resp.text == "response text"
    assert resp.input_tokens == 200
    assert resp.output_tokens == 100
    # Subscription path → cost_usd is ALWAYS 0.0
    assert resp.cost_usd == 0.0
    assert resp.duration_s >= 0
    # Metered-equivalent cost preserved in raw_response
    assert resp.raw_response["billing_path"] == "subscription"
    assert "api_metered_cost_usd" in resp.raw_response


def test_chat_marks_cached_when_cache_read_nonzero():
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True
    with patch(
        "subprocess.run",
        return_value=_mock_subprocess_response(cache_read=80),
    ):
        resp = asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="claude-opus-4-7",
            )
        )
    assert resp.cached is True
    assert resp.raw_response["cache_read_tokens"] == 80


def test_chat_uses_total_cost_usd_when_present():
    """When claude -p surfaces total_cost_usd, prefer it over the
    PRICING-table compute (catches model-tier + caching exactly)."""
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True
    with patch(
        "subprocess.run",
        return_value=_mock_subprocess_response(total_cost_usd=0.0042),
    ):
        resp = asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="claude-opus-4-7",
            )
        )
    assert resp.raw_response["api_metered_cost_usd"] == pytest.approx(0.0042)


def test_chat_falls_back_to_pricing_when_total_cost_usd_missing(caplog):
    """When claude -p response omits total_cost_usd (older CLI versions
    or some error paths), the adapter falls back to PRICING-table compute
    using input_tokens × in_per_mtok + output_tokens × out_per_mtok.

    Drop's PR #8 review carry-forward: this fallback path needs explicit
    test coverage symmetric with `test_chat_uses_total_cost_usd_when_present`.
    """
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True

    # Build a mock response WITHOUT total_cost_usd in the payload — claude
    # -p versions before --output-format=json stabilized often omitted it.
    payload = {
        "result": "fallback path test",
        "is_error": False,
        "api_error_status": None,
        "usage": {
            "input_tokens": 200,
            "output_tokens": 100,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        # NB: NO total_cost_usd key
        "duration_api_ms": 500,
        "session_id": "sid-fallback",
        "stop_reason": "end_turn",
    }
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = 0
    proc.stdout = json.dumps(payload)
    proc.stderr = ""

    with patch("subprocess.run", return_value=proc):
        resp = asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="claude-opus-4-7",
            )
        )

    # Falls back to PRICING table: opus-4-7 = ($5.00, $25.00) per Mtok
    # 200 in + 100 out = (200/1M × 5.00) + (100/1M × 25.00)
    #                  = 0.001 + 0.0025 = 0.0035
    expected = (200 / 1_000_000) * 5.00 + (100 / 1_000_000) * 25.00
    assert resp.raw_response["api_metered_cost_usd"] == pytest.approx(expected)
    # And subscription cost remains 0.0 regardless of fallback
    assert resp.cost_usd == 0.0


def test_chat_raises_on_nonzero_exit():
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True
    with patch(
        "subprocess.run",
        return_value=_mock_subprocess_response(returncode=1, stderr="auth failed"),
    ):
        with pytest.raises(AdapterError, match="exit=1"):
            asyncio.run(
                a.chat(
                    messages=[ChatMessage(role="user", content="x")],
                    model="claude-opus-4-7",
                )
            )


def test_chat_raises_on_is_error_true():
    """Even with returncode=0, claude -p can surface is_error=True in
    the JSON payload (e.g., model-side rate limit). Adapter should
    raise rather than return empty text."""
    a = ClaudeAdapter(claude_bin="/fake/claude")
    a._verified = True
    with patch(
        "subprocess.run",
        return_value=_mock_subprocess_response(
            is_error=True,
            api_error_status="rate_limited",
        ),
    ):
        with pytest.raises(AdapterError, match="is_error=True"):
            asyncio.run(
                a.chat(
                    messages=[ChatMessage(role="user", content="x")],
                    model="claude-opus-4-7",
                )
            )


def test_chat_raises_on_timeout():
    a = ClaudeAdapter(claude_bin="/fake/claude", timeout_seconds=1)
    a._verified = True
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
    ):
        with pytest.raises(AdapterError, match="timed out"):
            asyncio.run(
                a.chat(
                    messages=[ChatMessage(role="user", content="x")],
                    model="claude-opus-4-7",
                )
            )


def test_chat_raises_when_binary_missing():
    a = ClaudeAdapter(claude_bin="/nonexistent/claude")
    a._verified = True
    with patch("subprocess.run", side_effect=FileNotFoundError("no such")):
        with pytest.raises(AdapterError, match="binary not found"):
            asyncio.run(
                a.chat(
                    messages=[ChatMessage(role="user", content="x")],
                    model="claude-opus-4-7",
                )
            )


def test_chat_runs_verify_lazily_on_first_call():
    """verify() should fire once on first chat() call, not at
    construction."""
    a = ClaudeAdapter(claude_bin="/fake/claude")
    assert a._verified is False  # not yet verified

    with patch(
        "swarph_mesh.adapters.claude.verify_subscription_setup"
    ) as mock_verify, patch(
        "subprocess.run",
        return_value=_mock_subprocess_response(),
    ):
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x")],
                model="claude-opus-4-7",
            )
        )
        # Second call — verify shouldn't re-run
        asyncio.run(
            a.chat(
                messages=[ChatMessage(role="user", content="x2")],
                model="claude-opus-4-7",
            )
        )
    assert mock_verify.call_count == 1


# ---------------------------------------------------------------------------
# stream() — v0.4.0 raises NotImplementedError
# ---------------------------------------------------------------------------


def test_stream_raises_not_implemented():
    a = ClaudeAdapter(claude_bin="/fake")

    async def _consume():
        async for _ in a.stream(
            messages=[ChatMessage(role="user", content="x")],
            model="claude-opus-4-7",
        ):
            pass

    with pytest.raises(NotImplementedError, match="v0.4.0"):
        asyncio.run(_consume())


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registry_dispatches_claude_provider():
    from swarph_mesh.adapters import get_adapter, reset_registry

    reset_registry()
    try:
        a1 = get_adapter("claude")
        a2 = get_adapter("claude")
        assert isinstance(a1, ClaudeAdapter)
        assert a1 is a2  # singleton-per-provider
    finally:
        reset_registry()


def test_unknown_provider_error_lists_phase_4_remaining():
    """v0.4.0 error message: gemini + deepseek + claude shipped;
    OpenAI/Grok pending."""
    from swarph_mesh.adapters import get_adapter, reset_registry
    from swarph_mesh.exceptions import UnknownProvider

    reset_registry()
    with pytest.raises(UnknownProvider, match="OpenAI|Grok|gemini.*deepseek.*claude"):
        get_adapter("nonexistent-provider")
