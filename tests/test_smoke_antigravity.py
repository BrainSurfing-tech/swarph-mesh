"""Live smoke test for the Antigravity adapter — the #244 envelope gate.

The adapter's JSON parsing is written against the measured envelope
(``status`` / ``response`` / top-level ``thinking_tokens`` +
``cache_read_tokens``, card #244 msgs 37898/37911 + ``json:"..."`` struct
tags in the agy binary). This smoke is the falsifiability gate for that
shape on the REAL CLI: one $0 subscription call through firejail + agy,
then assert the durable attribution row — not the adapter return — carries
the #244 fields.

Gated on: agy binary, firejail binary, and the antigravity OAuth token all
present. Skipped on hosts without the subscription lane.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from swarph_mesh import ChatMessage, SwarphCall
from swarph_mesh.adapters import register_adapter, reset_registry
from swarph_mesh.adapters.antigravity import (
    AntigravityAdapter,
    _resolve_agy_bin,
    _resolve_firejail_bin,
)
from swarph_mesh.attribution import FileAttributionWriter
from swarph_mesh.hooks import default_hooks


def _can_run_smoke() -> bool:
    if not Path(_resolve_agy_bin()).exists():
        return False
    if not Path(_resolve_firejail_bin()).exists():
        return False
    token = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    return token.exists()


pytestmark = pytest.mark.skipif(
    not _can_run_smoke(),
    reason="Antigravity smoke needs agy + firejail binaries and "
    "~/.gemini/antigravity-cli/antigravity-oauth-token",
)


def test_244_envelope_gate_live(tmp_path):
    """One real subscription call; the durable row is the proof."""
    row_file = tmp_path / "attribution.jsonl"
    try:
        register_adapter("antigravity", AntigravityAdapter())
        sc = SwarphCall(
            provider="antigravity",
            caller="cli.smoke.antigravity244",
            hooks=default_hooks(writer=FileAttributionWriter(path=row_file)),
        )
        resp = asyncio.run(
            sc.chat([ChatMessage(role="user", content="Reply with exactly: pong")])
        )
    finally:
        reset_registry()

    assert resp.text  # the lane still answers under --output-format json

    row = json.loads(row_file.read_text().strip().splitlines()[0])
    # Envelope observations — printed so a human reading the smoke output
    # sees the MEASURED shape, not just a green dot.
    print(
        f"agy smoke: text={resp.text[:40]!r} thinking={row['thinking_tokens']} "
        f"cache_read={row['cache_read_tokens']} in={row['input_tokens']} "
        f"out={row['output_tokens']} cost_basis={row['cost_basis']}"
    )
    # #244 assertions: the fields exist on the row and obey the contract.
    # None is legal (a given call may carry no cache/thinking), but the
    # KEYS must be present and cost_basis must be the honest "unknown".
    for key in ("thinking_tokens", "cache_read_tokens",
                "cache_creation_1h", "cache_creation_5m"):
        assert key in row
        assert row[key] is None or isinstance(row[key], int)
    assert row["cost_usd"] == 0.0
    assert row["cost_basis"] == "unknown"
    assert row["extra"].get("billing_path") == "subscription"
