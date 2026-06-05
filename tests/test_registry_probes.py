"""Tests for the spec-driven gate-probe framework (P2 T2a).

Generalizes the hardcoded OpenClaw gate-probe harness
(swarph-shim/openclaw_gate_probes.py) to run the 7 gates against any
:class:`AdapterSpec`. Gates lacking a needed input return BLOCKED — never
FAIL, never a false PASS.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from swarph_mesh.registry import AdapterSpec, GateResult, GateStatus, probe_adapter


def _cli_spec(**overrides):
    base = dict(
        name="openclaw",
        kind="cli",
        version="0.1.0",
        license="MIT",
        headless_oneshot=True,
        input_channel="argv",
        max_prompt_bytes=4128,
        sandbox_class="firejail-fs-deny+net-allowed",
        billing_class="subscription_zero",
        intents=["DEEP_CODE", "CHIT_CHAT"],
    )
    base.update(overrides)
    return AdapterSpec(**base)


def _router_spec(**overrides):
    base = dict(
        name="openrouter",
        kind="router",
        version="1.0.0",
        license="Apache-2.0",
        headless_oneshot=True,
        billing_class="metered",
        intents=["DEEP_CODE"],
        models=["x/y", "a/b"],
    )
    base.update(overrides)
    return AdapterSpec(**base)


def test_g3_subscription_zero_passes_declared_only():
    res = probe_adapter(_cli_spec(billing_class="subscription_zero"))["G3"]
    assert isinstance(res, GateResult)
    assert res.status is GateStatus.PASS
    assert "declared-only" in res.detail


def test_g3_metered_router_blocked():
    res = probe_adapter(_router_spec(billing_class="metered"))["G3"]
    assert res.status is GateStatus.BLOCKED


def test_g5_provenance_pass_with_hex_sha(tmp_path):
    f = tmp_path / "source.mjs"
    f.write_bytes(b"console.log('hello');")
    res = probe_adapter(_cli_spec(), provenance_path=str(f))["G5"]
    assert res.status is GateStatus.PASS
    expected = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    assert expected in res.detail
    assert re.search(r"[0-9a-f]{16}", res.detail)


def test_g5_provenance_blocked_without_path():
    res = probe_adapter(_cli_spec())["G5"]
    assert res.status is GateStatus.BLOCKED


def test_g4_clean_config_dir_passes(tmp_path):
    res = probe_adapter(_cli_spec(), config_home=str(tmp_path))["G4"]
    assert res.status is GateStatus.PASS


def test_g4_contaminated_config_dir_fails(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# inherited")
    res = probe_adapter(_cli_spec(), config_home=str(tmp_path))["G4"]
    assert res.status is GateStatus.FAIL
    assert "CLAUDE.md" in res.detail


def test_g4_blocked_without_config_home():
    res = probe_adapter(_cli_spec())["G4"]
    assert res.status is GateStatus.BLOCKED


def test_g1_with_pong_runner_passes():
    res = probe_adapter(_cli_spec(), runner=lambda p: "PONG")["G1"]
    assert res.status is GateStatus.PASS


def test_g1_case_insensitive_runner_passes():
    res = probe_adapter(_cli_spec(), runner=lambda p: "...pong...")["G1"]
    assert res.status is GateStatus.PASS


def test_g1_without_runner_blocked():
    res = probe_adapter(_cli_spec())["G1"]
    assert res.status is GateStatus.BLOCKED
    assert "no runner" in res.detail.lower()


def test_g1b_router_skips():
    res = probe_adapter(_router_spec())["G1b"]
    assert res.status is GateStatus.SKIP


def test_g1b_cli_blocked_without_runner():
    res = probe_adapter(_cli_spec())["G1b"]
    assert res.status is GateStatus.BLOCKED
    assert "input_channel=argv" in res.detail
    assert "max_prompt_bytes=4128" in res.detail


def test_g1b_cli_with_runner_passes_informational():
    res = probe_adapter(_cli_spec(), runner=lambda p: "ok")["G1b"]
    assert res.status is GateStatus.PASS


def test_g2_always_blocked_drop_gated():
    res = probe_adapter(_cli_spec(), runner=lambda p: "PONG")["G2"]
    assert res.status is GateStatus.BLOCKED
    assert "2171" in res.detail


def test_g6_always_skip():
    res = probe_adapter(_cli_spec())["G6"]
    assert res.status is GateStatus.SKIP


def test_probe_returns_all_seven_gates():
    res = probe_adapter(_cli_spec())
    assert set(res) == {"G1", "G1b", "G2", "G3", "G4", "G5", "G6"}
