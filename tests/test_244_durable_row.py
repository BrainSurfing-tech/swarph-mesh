"""#244 can-fail gate: drive the REAL ``attribution_post_call`` hook and
assert the DURABLE ROW carries thinking_tokens, the cache split, cost_basis
and ``extra``.

>>> A TEST READING THE ADAPTER'S RETURN VALUE INSTEAD OF THE WRITTEN ROW
PASSES TODAY AND PROVES NOTHING. That is exactly how ``extra`` stayed dead
since v0.1 — the adapter populated it and every test looked at the adapter.
(card #244, msgs 37893/37911) <<<

Shape: real adapter (subprocess mocked with the MEASURED provider payload)
→ real SwarphCall → real attribution_post_call → real make_event → real
FileAttributionWriter → assertions read back the JSONL row from disk.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from swarph_mesh import ChatMessage, SwarphCall
from swarph_mesh.adapters import register_adapter, reset_registry
from swarph_mesh.attribution import FileAttributionWriter
from swarph_mesh.hooks import default_hooks


def _proc(stdout: str) -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = 0
    p.stdout = stdout
    p.stderr = ""
    return p


def _read_single_row(path) -> dict:
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one durable row, got {len(lines)}"
    return json.loads(lines[0])


# The measured claude -p --output-format=json payload (card #244 msg 37895:
# thinking reported as 0 on that call, 1h cache creation 48443, 5m 0,
# cache read 13547, total_cost_usd 0.48549 labeled "list" by the CLI).
_CLAUDE_PAYLOAD = {
    "result": "measured answer",
    "is_error": False,
    "usage": {
        "input_tokens": 4,
        "output_tokens": 190,
        "cache_read_input_tokens": 13547,
        "cache_creation_input_tokens": 48443,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 48443,
            "ephemeral_5m_input_tokens": 0,
        },
        "output_tokens_details": {"thinking_tokens": 0},
    },
    "total_cost_usd": 0.48549,
    "session_id": "sid-244",
    "stop_reason": "end_turn",
}


def test_durable_row_carries_244_fields_claude_lane(tmp_path):
    from swarph_mesh.adapters.claude import ClaudeAdapter

    adapter = ClaudeAdapter(claude_bin="/fake/claude")
    adapter._verified = True  # skip creds check — subprocess is mocked
    row_file = tmp_path / "attribution.jsonl"
    try:
        register_adapter("claude", adapter)
        sc = SwarphCall(
            provider="claude",
            caller="test.case.durable244",
            model="claude-opus-4-7",
            hooks=default_hooks(writer=FileAttributionWriter(path=row_file)),
        )
        with patch("subprocess.run", return_value=_proc(json.dumps(_CLAUDE_PAYLOAD))):
            resp = asyncio.run(sc.chat([ChatMessage(role="user", content="hi")]))
    finally:
        reset_registry()

    # sanity on the return value (NOT the proof — the row below is)
    assert resp.text == "measured answer"

    row = _read_single_row(row_file)
    assert row["thinking_tokens"] == 0          # reported zero — NOT null
    assert row["cache_read_tokens"] == 13547
    assert row["cache_creation_1h"] == 48443
    assert row["cache_creation_5m"] == 0        # reported zero — NOT null
    assert row["cost_usd"] == pytest.approx(0.48549)
    assert row["cost_basis"] == "list"
    # the dead-since-v0.1 field, alive: billing facts survive to the row
    assert row["extra"] == {"billing_path": "subscription"}


def test_durable_row_none_means_not_reported_grok_cli_lane(tmp_path):
    """A lane with NO stats output writes null (not 0) for every #244 token
    field, cost_basis="unknown" beside cost_usd=0.0 — the original defect
    (a bare 0.0 summable as measured-free) is structurally impossible."""
    from swarph_mesh.adapters.grok_cli import GrokCLIAdapter

    adapter = GrokCLIAdapter(
        grok_bin="/fake/grok", firejail_bin="/fake/firejail",
        prompt_dir=str(tmp_path / "prompts"),
    )
    row_file = tmp_path / "attribution.jsonl"
    try:
        register_adapter("grok-cli", adapter)
        sc = SwarphCall(
            provider="grok-cli",
            caller="test.case.durable244",
            model="grok-build",
            hooks=default_hooks(writer=FileAttributionWriter(path=row_file)),
        )
        with patch("subprocess.run", return_value=_proc("plain answer")), \
             patch("swarph_mesh.adapters.grok_cli._audit"):
            asyncio.run(sc.chat([ChatMessage(role="user", content="hi")]))
    finally:
        reset_registry()

    row = _read_single_row(row_file)
    assert row["thinking_tokens"] is None
    assert row["cache_read_tokens"] is None
    assert row["cache_creation_1h"] is None
    assert row["cache_creation_5m"] is None
    assert row["cost_usd"] == 0.0
    assert row["cost_basis"] == "unknown"
    assert row["extra"] == {"billing_path": "subscription"}


def test_durable_row_extra_carries_vibe_billing_facts(tmp_path, monkeypatch):
    """The full extra contract — {billing_path, max_price_usd,
    vendor_domicile} — harvested from the vibe lane's raw_response into the
    row. raw_response itself is stripped before TSDB write, so extra is the
    only durable carrier for these."""
    from swarph_mesh.adapters.vibe_cli import VibeCLIAdapter

    monkeypatch.setenv("MISTRAL_API_KEY", "fake-sub-key")
    adapter = VibeCLIAdapter(vibe_bin="/fake/venv/bin/vibe", firejail_bin="/fake/firejail")
    row_file = tmp_path / "attribution.jsonl"
    vibe_stdout = json.dumps([
        {"type": "message", "role": "assistant",
         "content": [{"type": "text", "text": "bonjour"}]},
    ])
    try:
        register_adapter("vibe-cli", adapter)
        sc = SwarphCall(
            provider="vibe-cli",
            caller="test.case.durable244",
            model="mistral-vibe",
            hooks=default_hooks(writer=FileAttributionWriter(path=row_file)),
        )
        with patch("subprocess.run", return_value=_proc(vibe_stdout)), \
             patch("swarph_mesh.adapters.vibe_cli._audit"):
            asyncio.run(sc.chat([ChatMessage(role="user", content="salut")]))
    finally:
        reset_registry()

    row = _read_single_row(row_file)
    assert row["extra"]["billing_path"] == "subscription"
    assert row["extra"]["max_price_usd"] == 0.25  # the enforceable ceiling
    assert row["extra"]["vendor_domicile"] == "FR"
    assert row["cost_basis"] == "unknown"
    assert row["thinking_tokens"] is None


def test_durable_row_antigravity_lane_carries_agy_stats(tmp_path):
    """The gemini lane the commander's priority names: agy's usage-nested
    thinking_tokens / cache_read_tokens reach the durable row; the split
    agy does not report stays null; cost_basis stays "unknown" from the
    adapter (the consumer-side price table upgrades it to "calculated")."""
    from swarph_mesh.adapters.antigravity import AntigravityAdapter

    adapter = AntigravityAdapter(agy_bin="/fake/agy", firejail_bin="/fake/firejail")
    row_file = tmp_path / "attribution.jsonl"
    agy_stdout = json.dumps({
        "status": "SUCCESS",
        "response": "gemini answer",
        # token stats nested under `usage` — the measured envelope
        # (lab-ovh msg 38001; the top-level shape from msg 37898 was
        # one nesting level too high)
        "usage": {
            "thinking_tokens": 21,
            "cache_read_tokens": 8129,
            "input_tokens": 40,
            "output_tokens": 60,
            "total_tokens": 100,
        },
    })
    try:
        register_adapter("antigravity", adapter)
        sc = SwarphCall(
            provider="antigravity",
            caller="test.case.durable244",
            model="gemini-3.5-flash",
            hooks=default_hooks(writer=FileAttributionWriter(path=row_file)),
        )
        with patch("subprocess.run", return_value=_proc(agy_stdout)), \
             patch("swarph_mesh.adapters.antigravity._audit"):
            asyncio.run(sc.chat([ChatMessage(role="user", content="hi")]))
    finally:
        reset_registry()

    row = _read_single_row(row_file)
    assert row["thinking_tokens"] == 21
    assert row["cache_read_tokens"] == 8129
    assert row["cache_creation_1h"] is None
    assert row["cache_creation_5m"] is None
    assert row["input_tokens"] == 40 and row["output_tokens"] == 60
    assert row["cost_usd"] == 0.0
    assert row["cost_basis"] == "unknown"
