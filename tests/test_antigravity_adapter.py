"""Tests for the Antigravity (firejail-sandboxed `agy -p`) adapter — offline.

Live smoke (real firejail + agy + ~/.gemini OAuth) lives in
``test_smoke_antigravity.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

_FIXTURES = Path(__file__).parent / "fixtures"

import pytest

from swarph_mesh.adapters.antigravity import (
    DEFAULT_MODEL,
    AntigravityAdapter,
    _EXTRA_SCRUB,
    _build_prompt,
    _firejail_argv,
    _scrubbed_env,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMAdapter


def test_adapter_satisfies_protocol():
    assert isinstance(AntigravityAdapter(agy_bin="/fake", firejail_bin="/fake"), LLMAdapter)


def test_provider_name_and_default_model():
    a = AntigravityAdapter(agy_bin="/fake", firejail_bin="/fake")
    assert a.name == "antigravity"
    assert a.default_model == DEFAULT_MODEL == "gemini-3.5-flash"


def test_api_key_kwarg_is_no_op_with_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        AntigravityAdapter(api_key="AIza-fake", agy_bin="/fake", firejail_bin="/fake")
    assert any("ignores api_key" in r.message for r in caplog.records)


# --- env scrub (the security-critical part) ---

def test_scrubbed_env_strips_extra_billing_vars(monkeypatch):
    """The GeminiCLIAdapter scrub-gap: GOOGLE_APPLICATION_CREDENTIALS + project
    vars don't end in _API_KEY so the suffix denylist misses them. This adapter
    must strip them explicitly to prevent a metered Vertex/GCP fallback."""
    for k in _EXTRA_SCRUB:
        monkeypatch.setenv(k, "LEAK_CANARY")
    monkeypatch.setenv("GEMINI_API_KEY", "LEAK_CANARY")
    e = _scrubbed_env()
    for k in _EXTRA_SCRUB:
        assert k not in e, f"{k} must be scrubbed"
    assert "GEMINI_API_KEY" not in e
    assert "PATH" in e  # not over-scrubbed


def test_extra_scrub_covers_gac():
    assert "GOOGLE_APPLICATION_CREDENTIALS" in _EXTRA_SCRUB


# --- firejail argv (the sandbox profile) ---

def test_firejail_argv_has_hardening_flags():
    argv = _firejail_argv("/usr/bin/firejail", "/home/u/.local/bin/agy", "hi")
    assert argv[0] == "/usr/bin/firejail"
    for flag in ("--seccomp", "--noroot", "--nonewprivs", "--caps.drop=all", "--private-tmp"):
        assert flag in argv, f"{flag} missing from sandbox profile"
    # agy binary is whitelisted + invoked; prompt passed via -p
    assert any(a.startswith("--whitelist=") and a.endswith("/.local/bin/agy") for a in argv)
    assert "/home/u/.local/bin/agy" in argv
    assert "-p" in argv and "hi" in argv
    # #244: JSON envelope carries the token stats plain text discards
    assert "--output-format" in argv and "json" in argv


def test_firejail_argv_whitelists_agy_runtime_dir():
    argv = _firejail_argv("/usr/bin/firejail", "/home/u/.local/bin/agy", "x")
    assert any(a.startswith("--whitelist=") and a.endswith("/.gemini/antigravity-cli") for a in argv)


# --- prompt builder ---

def test_build_prompt_multi_turn():
    out = _build_prompt(
        [ChatMessage(role="user", content="q1"), ChatMessage(role="assistant", content="a1")],
        system_prompt="sys",
    )
    assert out.startswith("sys")
    assert out.find("[USER]\nq1") < out.find("[ASSISTANT]\na1")


# --- chat() mocked ---

def _agy_json(**over):
    """The measured `agy -p --output-format json` envelope shape (#244):
    token stats live NESTED under `usage`, not at top level (measured live
    twice on lab-ovh, msg 38001). Overrides route to the block that owns
    the key: known usage keys into `usage`, everything else top-level."""
    usage = {
        "thinking_tokens": 21,
        "cache_read_tokens": 8129,
        "input_tokens": 12,
        "output_tokens": 34,
        "total_tokens": 46,  # measured: total = input + output, thinking excluded
    }
    payload = {"status": "SUCCESS", "response": "hello world", "usage": usage}
    for k, v in over.items():
        (usage if k in usage else payload)[k] = v
    return json.dumps(payload)


def _mock_proc(*, stdout=None, returncode=0, stderr=""):
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = _agy_json() if stdout is None else stdout
    p.stderr = stderr
    return p


def test_chat_returns_text_zero_cost_subscription():
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc()):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            r = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model="gemini-3.5-flash"))
    assert r.text == "hello world"
    assert r.cost_usd == 0.0
    assert r.cost_basis == "unknown"  # agy reports no cost figure (#244)
    assert r.input_tokens == 12 and r.output_tokens == 34
    assert r.raw_response["billing_path"] == "subscription"
    assert r.raw_response["sandbox"] == "firejail"


def test_chat_maps_token_stats_244():
    """The capture that used to be discarded: usage-nested thinking_tokens +
    cache_read_tokens map to the contract fields; the split agy does not
    report (cache_creation_*) stays None, not 0."""
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc()):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            r = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model=""))
    assert r.thinking_tokens == 21
    assert r.cache_read_tokens == 8129
    assert r.cache_creation_1h is None and r.cache_creation_5m is None
    assert r.cached is True  # cache_read_tokens > 0


def test_chat_reported_zero_stays_zero_not_none():
    """None = not reported; 0 = reported zero. The two must not collapse."""
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(
            stdout=_agy_json(thinking_tokens=0, cache_read_tokens=0))):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            r = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model=""))
    assert r.thinking_tokens == 0
    assert r.cache_read_tokens == 0
    assert r.cached is False


def test_chat_top_level_stats_raise_not_silently_zero():
    """The pre-fix shape assumption: token fields at TOP level, no `usage`.
    That shape must RAISE, not parse — a compatibility dual-read (or a
    manufactured-0 default) would mask the next wire-shape change the way
    the top-level read masked this one (lab-ovh msgs 38001/38009: every
    field parse-missed while the smoke greened)."""
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(
            stdout=json.dumps({"status": "SUCCESS", "response": "hi",
                               "thinking_tokens": 21, "cache_read_tokens": 8129,
                               "input_tokens": 12, "output_tokens": 34}))):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            with pytest.raises(AdapterError, match="usage"):
                asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model=""))


def test_chat_usage_without_token_counts_raises():
    """`usage` present but input/output unreadable → raise, never default.
    The default IS the defect (msg 38009): `.get("input_tokens", 0)` would
    manufacture the same zero one level down."""
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(
            stdout=json.dumps({"status": "SUCCESS", "response": "hi",
                               "usage": {"thinking_tokens": 5}}))):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            with pytest.raises(AdapterError, match="token counts"):
                asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model=""))


def test_chat_absent_optional_stats_stay_none():
    """usage carries the required counts but no thinking/cache keys —
    the two Optional contract fields stay None ("not reported")."""
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(
            stdout=json.dumps({"status": "SUCCESS", "response": "hi",
                               "usage": {"input_tokens": 12, "output_tokens": 34}}))):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            r = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model=""))
    assert r.thinking_tokens is None
    assert r.cache_read_tokens is None
    assert r.input_tokens == 12 and r.output_tokens == 34


@pytest.mark.parametrize("fixture,expect", [
    ("agy_envelope_pong.json",
     {"input": 14083, "output": 33, "thinking": 32, "cache_read": 0}),
    ("agy_envelope_reasoning.json",
     {"input": 14124, "output": 908, "thinking": 702, "cache_read": 0}),
])
def test_chat_parses_recorded_envelope_exactly(fixture, expect):
    """The CI-runnable can-fail (msg 38009): the smoke's `input_tokens > 0`
    discriminator only runs where the subscription lane exists, so the
    RECORDED envelopes (lab-ovh live calls, msgs 38001/38009; usage values
    exact-measured, conversation_id/duration redacted) are committed and
    asserted against EXACT values. Fails loudly on both the nesting bug
    and a manufactured default. Two fixtures because thinking_tokens
    32 -> 702 pins that thinking VARIES with the work — a measurement,
    not a constant."""
    raw = (_FIXTURES / fixture).read_text()
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(stdout=raw)):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            r = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model=""))
    assert r.input_tokens == expect["input"]
    assert r.output_tokens == expect["output"]
    assert r.thinking_tokens == expect["thinking"]
    assert r.cache_read_tokens == expect["cache_read"]  # a REPORTED zero, not absent
    assert r.cached is False  # cache_read == 0
    assert r.cost_usd == 0.0 and r.cost_basis == "unknown"


def test_chat_non_success_status_raises():
    """agy exits 0 even on failures — only explicit SUCCESS is success."""
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(
            stdout=_agy_json(status="PERMISSION_DENIED"))):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            with pytest.raises(AdapterError, match="status="):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_non_json_output_raises():
    """Pre-`--output-format` agy build or wire-shape change: fail loud, no
    silent plain-text fallback (that would re-open the #244 capture gap)."""
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(stdout="hello world\n")):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            with pytest.raises(AdapterError, match="failed to parse"):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_invokes_through_firejail_with_scrubbed_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/leak/sa.json")
    monkeypatch.setenv("GEMINI_API_KEY", "leak")
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env", {})
        return _mock_proc()

    with patch("subprocess.run", side_effect=fake_run):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))
    assert captured["argv"][0] == "/fake/firejail"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in captured["env"]
    assert "GEMINI_API_KEY" not in captured["env"]


def test_chat_raises_on_nonzero_exit():
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="sandbox denied")):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            with pytest.raises(AdapterError, match="exit=1"):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_raises_on_empty_response():
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(stdout="   ")):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            with pytest.raises(AdapterError, match="empty response"):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_timeout_raises():
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail", timeout_seconds=1)
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("agy", 1)):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            with pytest.raises(AdapterError, match="timed out"):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_cost_per_token_is_zero_subscription():
    a = AntigravityAdapter(agy_bin="/fake", firejail_bin="/fake")
    assert a.cost_per_token("anything") == (0.0, 0.0)
