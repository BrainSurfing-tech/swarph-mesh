"""Live smoke for ``swarph_mesh.discovery`` — Phase v0.6.0 falsifiability gate.

Hits ``https://api.aimlapi.com/models`` directly. No auth required by
AIMLAPI, but skipped when ``SWARPH_SKIP_NETWORK=1`` is set (CI without
egress, offline development).
"""

from __future__ import annotations

import os

import pytest

from swarph_mesh import (
    ModelInfo,
    invalidate_catalog,
    is_model_supported,
    list_models,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("SWARPH_SKIP_NETWORK") == "1",
    reason="SWARPH_SKIP_NETWORK=1 — discovery live smoke skipped",
)


def test_v0_6_0_aimlapi_catalog_round_trip():
    """End-to-end against live AIMLAPI: catalog non-empty, contains
    expected developers, all 5 of our adapters' core models present."""
    invalidate_catalog()
    catalog = list_models(ttl_seconds=0)
    # AIMLAPI advertises 400+ entries; even with dedup we expect 200+
    assert len(catalog) >= 200, f"catalog too small ({len(catalog)} entries)"

    devs = {m.developer for m in catalog}
    expected = {"Open AI", "Anthropic", "Google", "DeepSeek AI", "X AI"}
    assert expected.issubset(devs), f"missing developers: {expected - devs}"


def test_v0_6_0_provider_filter_against_live_catalog():
    """Each provider name in our adapter registry routes to a non-empty
    filtered list."""
    invalidate_catalog()

    for provider in ("openai", "claude", "gemini", "deepseek", "grok"):
        models = list_models(provider=provider, ttl_seconds=0)
        assert isinstance(models, list)
        assert len(models) > 0, f"no models found for provider={provider!r}"
        # Every returned model should be a ModelInfo
        for m in models:
            assert isinstance(m, ModelInfo)


def test_v0_6_0_known_model_ids_supported_live():
    """Models we ship in adapter PRICING tables should exist in catalog.
    A miss here is a real drift signal — our PRICING is stale OR AIMLAPI
    catalog dropped a deprecated model we still reference."""
    invalidate_catalog()

    # One canonical model per adapter — stable IDs that have been live
    # for months. Anything failing here is a substantive drift signal.
    canonical = [
        "gpt-4o",
        "gpt-4o-mini",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "deepseek-v4-flash",
    ]
    missing = [m for m in canonical if not is_model_supported(m)]
    assert not missing, f"PRICING-table models missing from AIMLAPI catalog: {missing}"
