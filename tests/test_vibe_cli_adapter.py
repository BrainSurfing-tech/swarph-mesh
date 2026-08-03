"""VibeCLIAdapter — the EU-domiciled, firejail-sandboxed ``vibe -p`` lane.

The tests concentrate on the four things that make this lane DIFFERENT from the
grok-cli lane it is modelled on, because those are the places a copied
implementation goes wrong:

  1. the credential is an ENV VAR the canonical scrub removes (grok's is a file),
  2. the response must be selected out of a JSON stream that also carries the
     model's private REASONING,
  3. the sandbox must not reach the operator's ``~/.vibe``,
  4. the cost ceiling must actually be passed.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from swarph_mesh.adapters.vibe_cli import (
    DEFAULT_MODEL,
    VibeCLIAdapter,
    _build_prompt,
    _extract_text,
    _firejail_argv,
    _resolve_vibe_bin,
    _scrubbed_env,
    _venv_root,
)
from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage


# --------------------------------------------------------------------------
# 1. The credential. This is the born-broken guard.
# --------------------------------------------------------------------------

def test_scrubbed_env_readds_the_allowlisted_mistral_key(monkeypatch):
    """The canonical scrub strips every ``*_API_KEY`` by suffix — including the
    one this lane authenticates with. If the re-add is ever dropped, the worker
    runs with no credential and fails at the PROVIDER, which reads as a broken
    adapter rather than an unconfigured box."""
    monkeypatch.setenv("MISTRAL_API_KEY", "sentinel-key")
    env = _scrubbed_env()
    assert env.get("MISTRAL_API_KEY") == "sentinel-key"


def test_canonical_scrub_alone_would_have_removed_it(monkeypatch):
    """Pins the PREMISE of the test above. If a future swarph_shared release
    stops stripping MISTRAL_API_KEY, the re-add becomes redundant rather than
    load-bearing — and this test tells us, instead of the knowledge silently
    rotting into a comment nobody trusts."""
    from swarph_shared import scrub_env_for_subprocess

    monkeypatch.setenv("MISTRAL_API_KEY", "sentinel-key")
    assert "MISTRAL_API_KEY" not in scrub_env_for_subprocess()


def test_scrubbed_env_drops_the_mistral_redirect_class(monkeypatch):
    """These do not end in ``_API_KEY``, so the suffix scrub misses them; an
    inherited value could point the agentic CLI at an attacker endpoint."""
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    for var in ("MISTRAL_API_BASE", "MISTRAL_BASE_URL", "MISTRAL_API_HOST"):
        monkeypatch.setenv(var, "http://evil.example")
    env = _scrubbed_env()
    for var in ("MISTRAL_API_BASE", "MISTRAL_BASE_URL", "MISTRAL_API_HOST"):
        assert var not in env, f"{var} survived the scrub"


def test_an_unknown_vibe_var_does_not_survive_the_scrub(monkeypatch):
    """>>> THE PREMISE PIN FOR THE OPEN NAMESPACE. <<<

    vibe's own --help declares ``VIBE_*  Override any config field``. Containment
    of an OPEN namespace cannot be an ENUMERATION — it fails open. The first cut
    of this adapter listed five vars and let ``VIBE_ACTIVE_MODEL`` (the vendor's
    own example, which redirects the model) through into the jail.

    This test names a var that does not exist today ON PURPOSE: the day vibe adds
    a config field, containment must cover it BY CONSTRUCTION rather than because
    someone re-read --help.
    """
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("VIBE_ACTIVE_MODEL", "local")
    monkeypatch.setenv("VIBE_SOME_FIELD_THAT_DOES_NOT_EXIST_YET", "attacker")
    monkeypatch.setenv("VIBE_HOME", "/operator/.vibe")
    env = _scrubbed_env()
    leaked = {k: v for k, v in env.items() if k.startswith("VIBE_")}
    assert leaked == {}, f"VIBE_* survived into the sandbox: {leaked}"


def test_the_credential_survives_the_prefix_scrub(monkeypatch):
    """The prefix scrub must not take the one var the lane needs — MISTRAL_API_KEY
    does not start with VIBE_, but ordering bugs are cheap to introduce here."""
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setenv("VIBE_ACTIVE_MODEL", "local")
    assert _scrubbed_env().get("MISTRAL_API_KEY") == "k"


def test_missing_credential_names_the_cause_not_a_generic_failure(monkeypatch):
    """The CANNOT-PROCEED branch. A missing credential must not present itself
    as a broken lane — that conflation is exactly the #243 defect."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    adapter = VibeCLIAdapter(vibe_bin="/nonexistent/vibe", firejail_bin="/nonexistent/firejail")
    with pytest.raises(AdapterError) as exc:
        asyncio.run(adapter.chat([ChatMessage(role="user", content="hi")], model=DEFAULT_MODEL))
    msg = str(exc.value)
    assert "MISTRAL_API_KEY" in msg
    assert "NOT a broken lane" in msg


def test_explicit_api_key_does_not_require_the_environment(monkeypatch):
    """An explicitly-passed key must satisfy the precondition without the
    adapter reaching into (or mutating) the process environment."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    adapter = VibeCLIAdapter(api_key="explicit", vibe_bin="/nonexistent/vibe",
                             firejail_bin="/nonexistent/firejail")
    with pytest.raises(AdapterError) as exc:
        asyncio.run(adapter.chat([ChatMessage(role="user", content="hi")], model=DEFAULT_MODEL))
    # It got PAST the credential gate and failed on the missing binary instead.
    assert "not found" in str(exc.value)
    assert "MISTRAL_API_KEY" not in os.environ


# --------------------------------------------------------------------------
# 2. Response extraction — an allowlist, because reasoning must never escape.
# --------------------------------------------------------------------------

_STREAM = json.dumps([
    {"type": "message", "role": "user",
     "content": [{"type": "text", "text": "the question"}]},
    {"type": "reasoning", "text": "PRIVATE deliberation that must not escape"},
    {"type": "message", "role": "assistant",
     "content": [{"type": "text", "text": "the answer"}]},
])


def test_extract_returns_only_the_assistant_message():
    assert _extract_text(_STREAM) == "the answer"


def test_extract_never_leaks_reasoning_or_the_user_echo():
    out = _extract_text(_STREAM)
    assert "PRIVATE" not in out
    assert "the question" not in out


def test_unknown_entry_types_are_excluded_by_default():
    """The selector is an ALLOWLIST. A future vibe release adding a new entry
    type must be EXCLUDED until someone decides otherwise — a denylist would
    admit it silently, which is the #70 lesson."""
    stream = json.dumps([
        {"type": "some_future_type", "role": "assistant",
         "content": [{"type": "text", "text": "unvetted"}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "text", "text": "vetted"}]},
    ])
    assert _extract_text(stream) == "vetted"


def test_multiple_assistant_messages_are_joined_in_order():
    stream = json.dumps([
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "first"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "second"}]},
    ])
    assert _extract_text(stream) == "first\nsecond"


def test_non_text_content_blocks_are_skipped():
    stream = json.dumps([
        {"type": "message", "role": "assistant",
         "content": [{"type": "image", "url": "x"}, {"type": "text", "text": "kept"}]},
    ])
    assert _extract_text(stream) == "kept"


def test_unparseable_output_refuses_loudly_and_shows_the_head():
    with pytest.raises(AdapterError) as exc:
        _extract_text("not json at all")
    assert "did not parse" in str(exc.value)
    assert "not json" in str(exc.value)


def test_a_json_object_is_refused_not_coerced():
    """``--output json`` is documented as a list. A dict is a CONTRACT CHANGE,
    not something to best-effort around."""
    with pytest.raises(AdapterError) as exc:
        _extract_text('{"type": "message"}')
    assert "expected a JSON list" in str(exc.value)


def test_malformed_entries_do_not_crash_extraction():
    stream = json.dumps(["a bare string", 42, None,
                         {"type": "message", "role": "assistant",
                          "content": [{"type": "text", "text": "survived"}]}])
    assert _extract_text(stream) == "survived"


# --------------------------------------------------------------------------
# 3. The sandbox.
# --------------------------------------------------------------------------

def _argv(tmp_path):
    return _firejail_argv(
        "/usr/bin/firejail", "/opt/venv/bin/vibe",
        vibe_home=str(tmp_path / "home"), workdir=str(tmp_path / "work"),
        max_price=0.25, max_turns=1,
    )


def test_sandbox_never_whitelists_the_operator_vibe_dir(tmp_path):
    """The whole statelessness claim rests on this. The grok lane HAD to
    whitelist ~/.grok and then blacklist its memory subdir; this lane must not
    reach the operator's dir at all."""
    argv = _argv(tmp_path)
    operator_dir = str(Path.home() / ".vibe")
    assert not any(operator_dir in a for a in argv), argv


def test_sandbox_carries_the_hardening_flags(tmp_path):
    argv = _argv(tmp_path)
    for flag in ("--private-tmp", "--caps.drop=all", "--nonewprivs", "--noroot", "--seccomp"):
        assert flag in argv


def test_tools_are_disabled_at_the_application_layer(tmp_path):
    """firejail seals the filesystem; this closes the agentic surface too."""
    argv = _argv(tmp_path)
    assert "--disabled-tools" in argv
    assert argv[argv.index("--disabled-tools") + 1] == "*"


def test_prompt_is_not_passed_on_argv(tmp_path):
    """``-p`` must carry NO positional text — the prompt goes on stdin, so it
    never lands in the process table or on disk."""
    argv = _argv(tmp_path)
    assert "-p" in argv
    assert argv[argv.index("-p") + 1].startswith("--")


def test_venv_root_is_whitelisted_not_just_the_script(tmp_path):
    """vibe is a python console-script: whitelisting only the script would jail
    away its interpreter and site-packages."""
    argv = _argv(tmp_path)
    assert "--whitelist=/opt/venv" in argv


def test_venv_root_derivation():
    assert _venv_root("/opt/venv/bin/vibe") == "/opt/venv"


# --------------------------------------------------------------------------
# 4. The cost ceiling — the rail no other lane has.
# --------------------------------------------------------------------------

def test_max_price_is_actually_passed(tmp_path):
    argv = _argv(tmp_path)
    assert "--max-price" in argv
    assert argv[argv.index("--max-price") + 1] == "0.25"


def test_max_price_is_instance_configurable(tmp_path):
    argv = _firejail_argv("/f", "/v/bin/vibe", vibe_home=str(tmp_path),
                          workdir=str(tmp_path), max_price=1.5, max_turns=3)
    assert argv[argv.index("--max-price") + 1] == "1.5"
    assert argv[argv.index("--max-turns") + 1] == "3"


# --------------------------------------------------------------------------
# Binary resolution + registry wiring.
# --------------------------------------------------------------------------

def test_relative_vibe_bin_is_refused(monkeypatch):
    """A relative override re-opens PATH-shadowing by an unrelated 'vibe'."""
    monkeypatch.setenv("VIBE_BIN", "vibe")
    with pytest.raises(AdapterError) as exc:
        _resolve_vibe_bin()
    assert "absolute path" in str(exc.value)


def test_absolute_vibe_bin_override_is_honoured(monkeypatch):
    monkeypatch.setenv("VIBE_BIN", "/custom/vibe")
    assert _resolve_vibe_bin() == "/custom/vibe"


def test_registry_returns_the_vibe_adapter():
    from swarph_mesh.adapters import get_adapter

    adapter = get_adapter("vibe-cli", api_key="k")
    assert isinstance(adapter, VibeCLIAdapter)
    assert adapter.name == "vibe-cli"


def test_cost_per_token_is_flat_rate():
    assert VibeCLIAdapter(api_key="k").cost_per_token(DEFAULT_MODEL) == (0.0, 0.0)


def test_prompt_rendering_matches_the_sibling_adapters():
    out = _build_prompt(
        [ChatMessage(role="user", content="q"), ChatMessage(role="assistant", content="a")],
        "SYS",
    )
    assert out == "SYS\n\n[USER]\nq\n\n[ASSISTANT]\na"


# --------------------------------------------------------------------------
# End-to-end shape, with the subprocess faked.
# --------------------------------------------------------------------------

def test_chat_pipes_prompt_on_stdin_and_cleans_up_ephemeral_dirs(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        seen["vibe_home"] = kwargs["env"].get("VIBE_HOME")
        return subprocess.CompletedProcess(argv, 0, stdout=_STREAM, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = VibeCLIAdapter(vibe_bin="/opt/venv/bin/vibe", firejail_bin="/usr/bin/firejail")
    resp = asyncio.run(adapter.chat([ChatMessage(role="user", content="the question")],
                                    model=DEFAULT_MODEL))

    assert resp.text == "the answer"
    assert "the question" in seen["input"]          # prompt went via stdin
    assert not any("the question" in a for a in seen["argv"])  # ...and NOT via argv
    assert seen["vibe_home"]
    assert not Path(seen["vibe_home"]).exists(), "ephemeral VIBE_HOME was not removed"

def test_vendor_domicile_and_processing_residency_are_separate_facts(monkeypatch):
    """>>> A COMPANY'S LEGAL DOMICILE IS NOT WHERE YOUR DATA IS PROCESSED. <<<

    State the fact we know (where the vendor is incorporated) and the unknown we
    cannot see (where processing/retention happen) as TWO fields, so nothing
    invites a consumer to read the first as the second.
    """
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout=_STREAM, stderr=""))
    raw = asyncio.run(VibeCLIAdapter(vibe_bin="/o/bin/vibe", firejail_bin="/f").chat(
        [ChatMessage(role="user", content="q")], model=DEFAULT_MODEL)).raw_response
    assert raw["vendor_domicile"] == "FR"
    assert raw["vendor_legal_entity"] == "Mistral AI SAS"
    assert raw["processing_residency"] == "unknown"
    assert raw["processing_residency_attested"] is False


def test_no_field_is_shaped_like_a_jurisdiction_selector(monkeypatch):
    """>>> PINS THE FRAME, NOT THE VALUE — THE CORRECTION REVIEW FORCED. <<<

    An earlier revision emitted ``jurisdiction_declared="eu-unattested"``. The
    VALUE was honestly qualified (``== "eu"`` failed closed) and the field was
    STILL wrong: it was SHAPED like a data-residency selector, so a prefix or
    ``contains`` match groups it as EU regardless of the suffix. **Fail-closed on
    a wrong axis is still wrong.**

    So assert the absence of the SHAPE: no jurisdiction-named key, and no string
    value a naive ``contains("eu")`` residency filter would sweep up.
    ``vendor_domicile="FR"`` is deliberately the COMPANY's country code — it
    neither contains "eu" nor names a jurisdiction.
    """
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout=_STREAM, stderr=""))
    raw = asyncio.run(VibeCLIAdapter(vibe_bin="/o/bin/vibe", firejail_bin="/f").chat(
        [ChatMessage(role="user", content="q")], model=DEFAULT_MODEL)).raw_response

    assert not any("jurisdiction" in str(k).lower() for k in raw), (
        "a jurisdiction-shaped key invites exactly the conflation this fix removed")
    assert not any("compliant" in str(k).lower() for k in raw)
    swept = {k: v for k, v in raw.items() if isinstance(v, str) and "eu" in v.lower()}
    assert swept == {}, f"a naive contains('eu') residency filter would sweep up: {swept}"


def test_the_docstring_does_not_promise_durability_raw_response_lacks(monkeypatch):
    """>>> raw_response IS DOCUMENTED IN types.py AS "stripped before TSDB write".
    <<< An earlier docstring said a router or an audit could "SELECT on it" — FALSE
    for exactly the durable consumers that would need it. The summary is part of
    the contract, so bind it: the emitted facts must appear in the docstring, and
    the docstring must not advertise the removed jurisdiction selector."""
    import swarph_mesh.adapters.vibe_cli as mod

    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout=_STREAM, stderr=""))
    raw = asyncio.run(VibeCLIAdapter(vibe_bin="/o/bin/vibe", firejail_bin="/f").chat(
        [ChatMessage(role="user", content="q")], model=DEFAULT_MODEL)).raw_response
    doc = mod.__doc__ or ""
    assert raw["vendor_domicile"] in doc
    assert "processing_residency" in doc
    assert "stripped before TSDB write" in doc, (
        "the docstring must say where these fields do NOT survive to")
    # The specific false promise that was there before: raw_response presented as
    # a durable surface a router or audit could select on.
    assert "SELECT on it" not in doc, (
        "raw_response is stripped before the attribution row is written — it "
        "cannot be advertised as a selectable audit surface")


def test_token_counts_are_zero_not_invented(monkeypatch):
    """vibe's JSON output has no usage block (verified by execution). Reporting
    anything but 0 would be manufacturing a number."""
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout=_STREAM, stderr=""))
    resp = asyncio.run(VibeCLIAdapter(vibe_bin="/o/bin/vibe", firejail_bin="/f").chat(
        [ChatMessage(role="user", content="q")], model=DEFAULT_MODEL))
    assert (resp.input_tokens, resp.output_tokens, resp.cost_usd) == (0, 0, 0.0)
    assert "unavailable" in resp.raw_response["token_stats"]


def test_empty_assistant_turn_explains_the_max_price_case(monkeypatch):
    """When the ceiling interrupts the session the assistant turn can be absent.
    That is the ceiling WORKING; the error must not read as a lane failure."""
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, stdout="[]", stderr=""))
    with pytest.raises(AdapterError) as exc:
        asyncio.run(VibeCLIAdapter(vibe_bin="/o/bin/vibe", firejail_bin="/f").chat(
            [ChatMessage(role="user", content="q")], model=DEFAULT_MODEL))
    assert "--max-price" in str(exc.value)


def test_nonzero_exit_bounds_the_stderr_tail(monkeypatch):
    """stderr can carry response fragments and lands in the PERSISTENT audit log
    as well as the raised error."""
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 3, stdout="", stderr="X" * 5000))
    with pytest.raises(AdapterError) as exc:
        asyncio.run(VibeCLIAdapter(vibe_bin="/o/bin/vibe", firejail_bin="/f").chat(
            [ChatMessage(role="user", content="q")], model=DEFAULT_MODEL))
    assert "exit=3" in str(exc.value)
    assert len(str(exc.value)) < 1200


def test_ephemeral_dirs_are_removed_even_when_the_call_fails(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    seen: dict = {}

    def boom(argv, **kwargs):
        seen["vibe_home"] = kwargs["env"].get("VIBE_HOME")
        raise FileNotFoundError("no firejail")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(AdapterError):
        asyncio.run(VibeCLIAdapter(vibe_bin="/o/bin/vibe", firejail_bin="/f").chat(
            [ChatMessage(role="user", content="q")], model=DEFAULT_MODEL))
    assert not Path(seen["vibe_home"]).exists()
