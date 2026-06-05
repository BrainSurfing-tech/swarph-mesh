"""T5a — worked instances proving the contract spans both kinds."""

from __future__ import annotations

from swarph_mesh.registry import (
    OPENCLAW_SPEC,
    OPENROUTER_SPEC,
    AdapterRegistry,
    register_builtin_instances,
)


def test_openclaw_is_valid_cli_spec():
    assert OPENCLAW_SPEC.kind == "cli"
    assert OPENCLAW_SPEC.billing_class == "subscription_zero"
    assert OPENCLAW_SPEC.name == "openclaw"


def test_openrouter_is_valid_router_spec():
    assert OPENROUTER_SPEC.kind == "router"
    assert "openai/gpt-5.5" in OPENROUTER_SPEC.models
    assert OPENROUTER_SPEC.billing_class == "metered"


def test_register_builtin_instances_registers_both():
    reg = AdapterRegistry()
    register_builtin_instances(reg, openclaw_runner=lambda p: "PONG")
    # both resolvable -> no raise; openclaw on an in-cap prompt
    assert reg.resolve("DEEP_CODE", prompt_bytes=100).name == "openclaw"


def test_resolve_prefers_zero_cost_openclaw_over_metered():
    reg = AdapterRegistry()
    register_builtin_instances(reg, openclaw_runner=lambda p: "PONG")
    spec = reg.resolve("DEEP_CODE", prompt_bytes=100)
    assert spec.name == "openclaw"
    assert spec.billing_class == "subscription_zero"


def test_zero_cost_still_wins_with_budget_ok():
    reg = AdapterRegistry()
    register_builtin_instances(reg, openclaw_runner=lambda p: "PONG")
    spec = reg.resolve("DEEP_CODE", prompt_bytes=100, budget_ok=True)
    assert spec.name == "openclaw"
