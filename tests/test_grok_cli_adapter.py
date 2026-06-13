"""Tests for the Grok-CLI (firejail-sandboxed `grok --prompt-file`) adapter — offline.

The subscription Grok lane (sibling of the metered ``grok`` adapter), mirroring
the Antigravity adapter shape. Live smoke (real firejail + grok + ~/.grok OIDC)
is exercised separately on a host with the CLI authed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from swarph_mesh.adapters.grok_cli import (
    DEFAULT_MODEL,
    GrokCLIAdapter,
    _EXTRA_SCRUB,
    _build_prompt,
    _firejail_argv,
    _scrubbed_env,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMAdapter


def test_adapter_satisfies_protocol():
    assert isinstance(GrokCLIAdapter(grok_bin="/fake", firejail_bin="/fake"), LLMAdapter)


def test_provider_name_and_default_model():
    a = GrokCLIAdapter(grok_bin="/fake", firejail_bin="/fake")
    assert a.name == "grok-cli"
    assert a.default_model == DEFAULT_MODEL == "grok-build"


def test_api_key_kwarg_is_no_op_with_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        GrokCLIAdapter(api_key="xai-fake", grok_bin="/fake", firejail_bin="/fake")
    assert any("ignores api_key" in r.message for r in caplog.records)


# --- env scrub (the security-critical part) ---

def test_scrubbed_env_strips_metered_xai_keys(monkeypatch):
    """A metered XAI_API_KEY / aliases must never reach grok, so it can only use
    the $0 OIDC session token (~/.grok/auth.json)."""
    for k in _EXTRA_SCRUB:
        monkeypatch.setenv(k, "LEAK_CANARY")
    e = _scrubbed_env()
    for k in _EXTRA_SCRUB:
        assert k not in e, f"{k} must be scrubbed"
    assert "PATH" in e  # not over-scrubbed


def test_extra_scrub_covers_xai_api_key():
    assert "XAI_API_KEY" in _EXTRA_SCRUB


# --- firejail argv (the sandbox profile) ---

def test_firejail_argv_has_hardening_flags():
    argv = _firejail_argv("/usr/bin/firejail", "/home/u/.local/bin/grok",
                          "/home/u/.grok/swarph-prompts/swarph-abc.md")
    assert argv[0] == "/usr/bin/firejail"
    for flag in ("--seccomp", "--noroot", "--nonewprivs", "--caps.drop=all", "--private-tmp"):
        assert flag in argv, f"{flag} missing from sandbox profile"
    # grok binary is whitelisted + invoked; prompt passed via --prompt-file
    assert any(a.startswith("--whitelist=") and a.endswith("/.local/bin/grok") for a in argv)
    assert "/home/u/.local/bin/grok" in argv
    assert "--prompt-file" in argv
    assert "/home/u/.grok/swarph-prompts/swarph-abc.md" in argv
    assert "--output-format" in argv


def test_firejail_argv_whitelists_grok_runtime_and_prompt_dir():
    argv = _firejail_argv("/usr/bin/firejail", "/home/u/.local/bin/grok",
                          "/tmp/pd/swarph-x.md")
    # ~/.grok holds auth + sessions
    assert any(a.startswith("--whitelist=") and a.endswith("/.grok") for a in argv)
    # the prompt file's own dir must be reachable inside the sandbox
    assert "--whitelist=/tmp/pd" in argv


def test_firejail_argv_is_stateless_no_memory():
    """Stateless-by-construction: cross-session memory + subagents disabled so a
    mesh call never touches an operator's personal grok memory."""
    argv = _firejail_argv("/usr/bin/firejail", "/grok", "/pd/p.md")
    assert "--no-memory" in argv
    assert "--no-subagents" in argv


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


def test_chat_returns_text_zero_cost_subscription(tmp_path):
    a = GrokCLIAdapter(grok_bin="/fake/grok", firejail_bin="/fake/firejail",
                       prompt_dir=str(tmp_path))
    with patch("subprocess.run", return_value=_mock_proc(stdout="hello world\n")):
        with patch("swarph_mesh.adapters.grok_cli._audit"):
            r = asyncio.run(a.chat([ChatMessage(role="user", content="hi")], model="grok-build"))
    assert r.text == "hello world"
    assert r.cost_usd == 0.0
    assert r.input_tokens == 0 and r.output_tokens == 0
    assert r.raw_response["billing_path"] == "subscription"
    assert r.raw_response["sandbox"] == "firejail"


def test_chat_writes_prompt_file_then_cleans_up(tmp_path):
    """The prompt is written to a file in prompt_dir (reachable inside the
    sandbox), and removed after the call regardless of outcome."""
    a = GrokCLIAdapter(grok_bin="/fake/grok", firejail_bin="/fake/firejail",
                       prompt_dir=str(tmp_path))
    seen = {}

    def fake_run(argv, **kw):
        pf = argv[argv.index("--prompt-file") + 1]
        seen["existed_during_run"] = os.path.exists(pf)
        seen["pf"] = pf
        return _mock_proc(stdout="done")

    with patch("subprocess.run", side_effect=fake_run):
        with patch("swarph_mesh.adapters.grok_cli._audit"):
            asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))
    assert seen["existed_during_run"] is True
    assert not os.path.exists(seen["pf"])  # cleaned up


def test_chat_invokes_through_firejail_with_scrubbed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_API_KEY", "leak")
    a = GrokCLIAdapter(grok_bin="/fake/grok", firejail_bin="/fake/firejail",
                       prompt_dir=str(tmp_path))
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env", {})
        return _mock_proc()

    with patch("subprocess.run", side_effect=fake_run):
        with patch("swarph_mesh.adapters.grok_cli._audit"):
            asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))
    assert captured["argv"][0] == "/fake/firejail"
    assert "XAI_API_KEY" not in captured["env"]


def test_chat_raises_on_nonzero_exit(tmp_path):
    a = GrokCLIAdapter(grok_bin="/fake/grok", firejail_bin="/fake/firejail",
                       prompt_dir=str(tmp_path))
    with patch("subprocess.run", return_value=_mock_proc(returncode=1, stderr="sandbox denied")):
        with patch("swarph_mesh.adapters.grok_cli._audit"):
            with pytest.raises(AdapterError, match="exit=1"):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_raises_on_empty_response(tmp_path):
    a = GrokCLIAdapter(grok_bin="/fake/grok", firejail_bin="/fake/firejail",
                       prompt_dir=str(tmp_path))
    with patch("subprocess.run", return_value=_mock_proc(stdout="   ")):
        with patch("swarph_mesh.adapters.grok_cli._audit"):
            with pytest.raises(AdapterError, match="empty response"):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_timeout_raises(tmp_path):
    a = GrokCLIAdapter(grok_bin="/fake/grok", firejail_bin="/fake/firejail",
                       timeout_seconds=1, prompt_dir=str(tmp_path))
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("grok", 1)):
        with patch("swarph_mesh.adapters.grok_cli._audit"):
            with pytest.raises(AdapterError, match="timed out"):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))


def test_chat_cleans_up_prompt_file_on_failure(tmp_path):
    """Even on nonzero exit, the temp prompt file is removed (finally block)."""
    a = GrokCLIAdapter(grok_bin="/fake/grok", firejail_bin="/fake/firejail",
                       prompt_dir=str(tmp_path))
    seen = {}

    def fake_run(argv, **kw):
        seen["pf"] = argv[argv.index("--prompt-file") + 1]
        return _mock_proc(returncode=2, stderr="boom")

    with patch("subprocess.run", side_effect=fake_run):
        with patch("swarph_mesh.adapters.grok_cli._audit"):
            with pytest.raises(AdapterError):
                asyncio.run(a.chat([ChatMessage(role="user", content="x")], model=""))
    assert not os.path.exists(seen["pf"])


def test_cost_per_token_is_zero_subscription():
    a = GrokCLIAdapter(grok_bin="/fake", firejail_bin="/fake")
    assert a.cost_per_token("anything") == (0.0, 0.0)


# --- registry wiring ---

def test_registry_resolves_grok_cli():
    from swarph_mesh.adapters import get_adapter, reset_registry
    try:
        a = get_adapter("grok-cli")
        assert a.name == "grok-cli"
        assert isinstance(a, GrokCLIAdapter)
    finally:
        reset_registry()
