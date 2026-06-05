"""Tests for the AdapterRegistry (P2 T3).

The registry registers probe-validated adapters (fail-loud, no partial load)
and resolves a request to a lane intent-first + $0-first, with the agy-cap
filter dropping cli specs whose byte-cap is below the prompt. probe_all
re-probes every registered adapter in parallel, fail-loud.
"""

from __future__ import annotations

import pytest

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.registry import AdapterSpec
from swarph_mesh.registry.registry import AdapterRegistry, NoLaneError


def _claude_sub(**overrides):
    """subscription_zero cli, DEEP_CODE, generous byte-cap."""
    base = dict(
        name="claude-sub",
        kind="cli",
        version="2.1.0",
        license="proprietary",
        headless_oneshot=True,
        input_channel="stdin",
        max_prompt_bytes=1_000_000,
        sandbox_class="firejail-fs-deny+net-allowed",
        billing_class="subscription_zero",
        intents=["DEEP_CODE"],
    )
    base.update(overrides)
    return AdapterSpec(**base)


def _openrouter(**overrides):
    """metered router, DEEP_CODE, models catalog."""
    base = dict(
        name="openrouter",
        kind="router",
        version="1.0.0",
        license="Apache-2.0",
        headless_oneshot=True,
        billing_class="metered",
        intents=["DEEP_CODE"],
        models=["x/y"],
    )
    base.update(overrides)
    return AdapterSpec(**base)


def _nemotron(**overrides):
    """local_zero cli, CHIT_CHAT, 8000-byte cap (the agy-cap class)."""
    base = dict(
        name="nemotron",
        kind="cli",
        version="0.1.0",
        license="open",
        headless_oneshot=True,
        input_channel="argv",
        max_prompt_bytes=8000,
        sandbox_class="firejail-fs-deny+net-allowed",
        billing_class="local_zero",
        intents=["CHIT_CHAT"],
    )
    base.update(overrides)
    return AdapterSpec(**base)


_PONG = lambda p: "PONG"


def test_register_clean_spec_stores_it():
    reg = AdapterRegistry()
    reg.register(_claude_sub(), runner=_PONG)
    got = reg.resolve("DEEP_CODE", prompt_bytes=100)
    assert got.name == "claude-sub"


def test_resolve_prefers_subscription_zero_over_metered():
    reg = AdapterRegistry()
    reg.register(_claude_sub(), runner=_PONG)
    reg.register(_openrouter(), runner=_PONG)
    got = reg.resolve("DEEP_CODE", prompt_bytes=100)
    assert got.name == "claude-sub"  # $0-first, NOT the metered one


def test_resolve_zero_wins_even_when_budget_ok():
    reg = AdapterRegistry()
    reg.register(_claude_sub(), runner=_PONG)
    reg.register(_openrouter(), runner=_PONG)
    got = reg.resolve("DEEP_CODE", prompt_bytes=100, budget_ok=True)
    assert got.name == "claude-sub"


def test_metered_only_blocked_without_budget():
    reg = AdapterRegistry()
    reg.register(_openrouter(), runner=_PONG)
    with pytest.raises(NoLaneError):
        reg.resolve("DEEP_CODE", prompt_bytes=100, budget_ok=False)


def test_metered_only_resolves_with_budget():
    reg = AdapterRegistry()
    reg.register(_openrouter(), runner=_PONG)
    got = reg.resolve("DEEP_CODE", prompt_bytes=100, budget_ok=True)
    assert got.name == "openrouter"


def test_cap_filter_drops_over_cap_cli():
    reg = AdapterRegistry()
    reg.register(_nemotron(), runner=_PONG)  # 8000-byte cap
    # under cap → resolves
    assert reg.resolve("CHIT_CHAT", prompt_bytes=100).name == "nemotron"
    # over cap → dropped → no lane
    with pytest.raises(NoLaneError):
        reg.resolve("CHIT_CHAT", prompt_bytes=9000)


def test_register_fail_loud_on_gate_fail(tmp_path):
    # a CLAUDE.md in the config_home makes G4 FAIL → register must raise
    (tmp_path / "CLAUDE.md").write_text("inherited!")
    reg = AdapterRegistry()
    with pytest.raises(AdapterError):
        reg.register(_claude_sub(), runner=_PONG, config_home=str(tmp_path))
    # no partial load: nothing resolvable
    with pytest.raises(NoLaneError):
        reg.resolve("DEEP_CODE", prompt_bytes=100)


def test_on_register_hook_fires():
    seen = []
    reg = AdapterRegistry(on_register=seen.append)
    spec = _claude_sub()
    reg.register(spec, runner=_PONG)
    assert seen == [spec]


def test_resolve_best_prefers_model_name():
    reg = AdapterRegistry()
    reg.register(_openrouter(models=["x/y"]), runner=_PONG)
    reg.register(_openrouter(name="other-router", models=["a/b"]), runner=_PONG)
    got = reg.resolve("DEEP_CODE", prompt_bytes=100, model="a/b", budget_ok=True)
    assert got.name == "other-router"


def test_probe_all_returns_dict_keyed_by_name():
    reg = AdapterRegistry()
    reg.register(_claude_sub(), runner=_PONG)
    reg.register(_openrouter(), runner=_PONG)
    out = reg.probe_all()
    assert set(out) == {"claude-sub", "openrouter"}
    assert "G1" in out["claude-sub"]
