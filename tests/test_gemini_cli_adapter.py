"""Tests for the Gemini-CLI subscription adapter — offline, mocked subprocess.

The Google mirror of ``test_claude_adapter.py``. Live smoke (real
``gemini`` binary + ~/.gemini/oauth_creds.json) lives in
``test_smoke_gemini_cli.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from swarph_mesh.adapters.gemini_cli import (
    PRICING,
    GeminiCLIAdapter,
    _aggregate_tokens,
    _build_prompt,
    _parse_gemini_json,
    _resolve_gemini_bin,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMAdapter


# ---------------------------------------------------------------------------
# Protocol fit
# ---------------------------------------------------------------------------


def test_adapter_satisfies_protocol():
    a = GeminiCLIAdapter(gemini_bin="/fake/path")
    assert isinstance(a, LLMAdapter)


def test_provider_name():
    a = GeminiCLIAdapter(gemini_bin="/fake")
    assert a.name == "gemini-cli"


def test_api_key_kwarg_is_no_op_with_warning(caplog):
    """Passing an api_key logs a warning but doesn't error — subscription
    path uses ~/.gemini/oauth_creds.json, not env keys."""
    import logging

    with caplog.at_level(logging.WARNING):
        GeminiCLIAdapter(api_key="AIza-fake-test-key", gemini_bin="/fake")
    assert any("ignores api_key" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# gemini_bin resolution
# ---------------------------------------------------------------------------


def test_gemini_bin_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_BIN", "/custom/path/gemini")
    assert _resolve_gemini_bin() == "/custom/path/gemini"


def test_gemini_bin_prefers_usr_local(monkeypatch):
    """Unlike Claude, the gemini CLI installs to /usr/local/bin by default
    (npm global), so the system path wins over $HOME/.local."""
    monkeypatch.delenv("GEMINI_BIN", raising=False)
    with patch.object(Path, "exists", return_value=True):
        assert _resolve_gemini_bin() == "/usr/local/bin/gemini"


# ---------------------------------------------------------------------------
# Cost (metered-equivalent for subscription auditors)
# ---------------------------------------------------------------------------


def test_cost_per_token_returns_tuple():
    a = GeminiCLIAdapter(gemini_bin="/fake")
    inp, out = a.cost_per_token("gemini-2.5-pro")
    assert (inp, out) == (1.25, 5.00)


def test_cost_per_token_unknown_model_uses_default():
    a = GeminiCLIAdapter(gemini_bin="/fake")
    assert a.cost_per_token("gemini-future-2027") == PRICING["_default"]


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def test_build_prompt_with_system():
    out = _build_prompt(
        [ChatMessage(role="user", content="hi")], system_prompt="be terse"
    )
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
    assert out.find("[USER]\nq1") < out.find("[ASSISTANT]\na1")
    assert out.find("[ASSISTANT]\na1") < out.find("[USER]\nq2")


# ---------------------------------------------------------------------------
# JSON parser + token aggregation
# ---------------------------------------------------------------------------


def test_parse_gemini_json_happy_path():
    out = _parse_gemini_json(json.dumps({"response": "hi", "stats": {}}))
    assert out["response"] == "hi"


def test_parse_gemini_json_empty_raises():
    with pytest.raises(AdapterError, match="empty response"):
        _parse_gemini_json("")


def test_parse_gemini_json_malformed_raises():
    with pytest.raises(AdapterError, match="failed to parse"):
        _parse_gemini_json("{not valid json")


def test_aggregate_tokens_sums_across_models():
    """The CLI may invoke a utility-router model plus the main model;
    honest accounting sums prompt(in) + candidates(out) + cached."""
    stats = {
        "models": {
            "gemini-3.1-flash-lite": {
                "tokens": {"prompt": 2986, "candidates": 48, "cached": 0}
            },
            "gemini-3-flash-preview": {
                "tokens": {"prompt": 10825, "candidates": 1, "cached": 7865}
            },
        }
    }
    inp, out, cached = _aggregate_tokens(stats)
    assert inp == 2986 + 10825
    assert out == 48 + 1
    assert cached == 7865


def test_aggregate_tokens_empty_is_zero():
    assert _aggregate_tokens({}) == (0, 0, 0)


# ---------------------------------------------------------------------------
# chat() — subprocess invocation (mocked)
# ---------------------------------------------------------------------------


def _mock_proc(*, response="ok", returncode=0, stderr="",
               models=None) -> MagicMock:
    """Build a MagicMock with the gemini -p -o json shape."""
    if models is None:
        models = {"gemini-3-flash": {"tokens": {"prompt": 100, "candidates": 50, "cached": 0}}}
    payload = {"session_id": "sid-1", "response": response, "stats": {"models": models}}
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = json.dumps(payload)
    proc.stderr = stderr
    return proc


def test_chat_invokes_gemini_with_correct_argv():
    a = GeminiCLIAdapter(gemini_bin="/fake/gemini")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _mock_proc(response="hello")

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model="gemini-3-flash"))
    argv = captured["argv"]
    assert argv[0] == "/fake/gemini"
    assert "-p" in argv
    assert "-o" in argv and "json" in argv
    assert "-m" in argv and "gemini-3-flash" in argv


def test_chat_omits_model_flag_when_empty():
    """Empty model => let the CLI pick its subscription default (no -m)."""
    a = GeminiCLIAdapter(gemini_bin="/fake/gemini")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _mock_proc()

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model=""))
    assert "-m" not in captured["argv"]


def test_chat_strips_gemini_keys_from_subprocess_env(monkeypatch):
    """Critical billing-leak prevention: GEMINI_API_KEY / GOOGLE_API_KEY
    must NEVER reach the subprocess — they'd flip the CLI to metered."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-fake-leak")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-fake-leak")
    a = GeminiCLIAdapter(gemini_bin="/fake/gemini")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _mock_proc()

    with patch("subprocess.run", side_effect=fake_run):
        asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))
    assert "GEMINI_API_KEY" not in captured["env"]
    assert "GOOGLE_API_KEY" not in captured["env"]
    assert "PATH" in captured["env"]  # CLI still needs PATH


def test_chat_extracts_usage_and_zero_cost():
    a = GeminiCLIAdapter(gemini_bin="/fake/gemini")
    with patch("subprocess.run", return_value=_mock_proc(
        response="response text",
        models={"gemini-3-flash": {"tokens": {"prompt": 200, "candidates": 100, "cached": 0}}},
    )):
        resp = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model="gemini-3-flash"))
    assert resp.text == "response text"
    assert resp.input_tokens == 200
    assert resp.output_tokens == 100
    assert resp.cost_usd == 0.0  # subscription — always 0.0
    assert resp.raw_response["billing_path"] == "subscription"
    assert "api_metered_cost_usd" in resp.raw_response


def test_chat_marks_cached_when_cached_nonzero():
    a = GeminiCLIAdapter(gemini_bin="/fake/gemini")
    with patch("subprocess.run", return_value=_mock_proc(
        models={"m": {"tokens": {"prompt": 50, "candidates": 5, "cached": 7865}}},
    )):
        resp = asyncio.run(a.chat([ChatMessage(role="user", content="x")], model="m"))
    assert resp.cached is True
    assert resp.raw_response["cached_tokens"] == 7865


def test_chat_raises_on_nonzero_exit():
    a = GeminiCLIAdapter(gemini_bin="/fake/gemini")
    with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="auth failed")):
        with pytest.raises(AdapterError, match="exit=1"):
            asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_raises_on_missing_binary():
    a = GeminiCLIAdapter(gemini_bin="/no/such/gemini")
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(AdapterError, match="binary not found"):
            asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_prices_actual_model_not_requested():
    """Subscription tier can silently downgrade pro→flash; pricing AND
    raw_response['model'] must reflect the model ACTUALLY run (stats), not the
    requested one (adversarial-sweep MED — was pricing requested-pro vs flash)."""
    from swarph_mesh.adapters.gemini_cli import PRICING
    a = GeminiCLIAdapter(gemini_bin="/fake/gemini")
    # stats says FLASH ran, but the caller asked for PRO
    proc = _mock_proc(models={"gemini-3-flash":
                              {"tokens": {"prompt": 1000, "candidates": 500, "cached": 0}}})
    with patch("subprocess.run", return_value=proc):
        resp = asyncio.run(a.chat([ChatMessage(role="user", content="hi")],
                                  model="gemini-2.5-pro"))
    assert resp.raw_response["model"] == "gemini-3-flash"          # actual, not requested
    fin, fout = PRICING.get("gemini-3-flash", PRICING["_default"])
    expected = (1000 / 1e6) * fin + (500 / 1e6) * fout            # priced as flash
    assert abs(resp.raw_response["api_metered_cost_usd"] - expected) < 1e-9
