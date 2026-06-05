"""Tests for AdapterSpec — the open adapter contract (P2 T1).

AdapterSpec generalizes the hardcoded ``adapters/_REGISTRY`` into a
declared+probed capability record. This file covers the kind-conditional
node-vs-lane (§0) validation; probing lands in a later task.
"""

from __future__ import annotations

import pytest

from swarph_mesh.registry import AdapterSpec


def test_valid_cli_spec_constructs():
    spec = AdapterSpec(
        name="openclaw",
        kind="cli",
        version="0.1.0",
        license="MIT",
        headless_oneshot=True,
        input_channel="argv",
        max_prompt_bytes=120000,
        sandbox_class="firejail-fs-deny+net-allowed",
        billing_class="subscription_zero",
        intents=["DEEP_CODE", "CHIT_CHAT"],
    )
    assert spec.name == "openclaw"
    assert spec.kind == "cli"
    assert spec.input_channel == "argv"
    assert spec.max_prompt_bytes == 120000
    assert spec.models == []


def test_valid_router_spec_constructs():
    spec = AdapterSpec(
        name="openrouter",
        kind="router",
        version="1.0.0",
        license="Apache-2.0",
        headless_oneshot=True,
        billing_class="metered",
        intents=["DEEP_CODE"],
        models=["x/y", "a/b"],
    )
    assert spec.kind == "router"
    assert spec.models == ["x/y", "a/b"]
    assert spec.input_channel is None
    assert spec.max_prompt_bytes is None


def test_cli_without_input_channel_raises():
    with pytest.raises(ValueError):
        AdapterSpec(
            name="openclaw",
            kind="cli",
            version="0.1.0",
            license="MIT",
            headless_oneshot=True,
            max_prompt_bytes=120000,
            billing_class="subscription_zero",
            intents=["DEEP_CODE"],
        )


def test_cli_with_models_raises():
    with pytest.raises(ValueError):
        AdapterSpec(
            name="openclaw",
            kind="cli",
            version="0.1.0",
            license="MIT",
            headless_oneshot=True,
            input_channel="argv",
            max_prompt_bytes=120000,
            billing_class="subscription_zero",
            intents=["DEEP_CODE"],
            models=["x/y"],
        )


def test_router_with_empty_models_raises():
    with pytest.raises(ValueError):
        AdapterSpec(
            name="openrouter",
            kind="router",
            version="1.0.0",
            license="Apache-2.0",
            headless_oneshot=True,
            billing_class="metered",
            intents=["DEEP_CODE"],
            models=[],
        )


def test_router_with_sandbox_class_raises():
    with pytest.raises(ValueError):
        AdapterSpec(
            name="openrouter",
            kind="router",
            version="1.0.0",
            license="Apache-2.0",
            headless_oneshot=True,
            billing_class="metered",
            intents=["DEEP_CODE"],
            models=["x/y"],
            sandbox_class="firejail-fs-deny+net-allowed",
        )
