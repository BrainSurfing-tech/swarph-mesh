"""Tests for the Antigravity (firejail-sandboxed `agy -p`) adapter — offline.

Live smoke (real firejail + agy + ~/.gemini OAuth) lives in
``test_smoke_antigravity.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock, patch

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

def _mock_proc(*, stdout="OK", returncode=0, stderr=""):
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def test_chat_returns_text_zero_cost_subscription():
    a = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    with patch("subprocess.run", return_value=_mock_proc(stdout="hello world\n")):
        with patch("swarph_mesh.adapters.antigravity._audit"):
            r = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model="gemini-3.5-flash"))
    assert r.text == "hello world"
    assert r.cost_usd == 0.0
    assert r.input_tokens == 0 and r.output_tokens == 0
    assert r.raw_response["billing_path"] == "subscription"
    assert r.raw_response["sandbox"] == "firejail"


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
