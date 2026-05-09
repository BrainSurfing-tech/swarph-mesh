"""v0.6.2 — OpenAI full PRICING catch-up + drop's #745 carry-forwards.

Three classes of test:

1. New OpenAI PRICING entries from authoritative openai.com/api/pricing
   source (commander-pasted 2026-05-09): gpt-5.1/5.4/5.5 families,
   gpt-5-pro, o1-pro, o3-pro.
2. o3 over-attribution correction (was (10, 40), real (2, 8)).
3. v0.6.2 carry-forwards from drop DM #745:
   - Centralized normalizers in discovery.py
   - Shared retirement registry pattern
"""

from __future__ import annotations

import datetime
import pytest


# ---------------------------------------------------------------------------
# OpenAI PRICING — new entries
# ---------------------------------------------------------------------------


def test_openai_gpt_5_5_family():
    """v0.6.2 NEW: gpt-5.5 + gpt-5.5-pro from authoritative openai.com page."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-5.5"] == (5.00, 30.00)
    assert PRICING["gpt-5.5-pro"] == (30.00, 180.00)


def test_openai_gpt_5_4_family():
    """v0.6.2 NEW: gpt-5.4 family (full lineup with mini/nano/pro)."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-5.4"] == (2.50, 15.00)
    assert PRICING["gpt-5.4-mini"] == (0.75, 4.50)
    assert PRICING["gpt-5.4-nano"] == (0.20, 1.25)
    assert PRICING["gpt-5.4-pro"] == (30.00, 180.00)


def test_openai_gpt_5_1_family():
    """v0.6.2 NEW: gpt-5.1 + gpt-5.1-codex variants."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-5.1"] == (1.25, 10.00)
    assert PRICING["gpt-5.1-codex"] == (1.25, 10.00)
    assert PRICING["gpt-5.1-codex-mini"] == (0.25, 2.00)


def test_openai_gpt_5_pro():
    """v0.6.2 NEW: gpt-5-pro premium tier."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-5-pro"] == (15.00, 120.00)


def test_openai_premium_reasoner_tier():
    """v0.6.2 NEW: o1-pro + o3-pro premium reasoner tiers."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["o1-pro"] == (150.00, 600.00)  # premium reasoner
    assert PRICING["o3-pro"] == (20.00, 80.00)


# ---------------------------------------------------------------------------
# o3 over-attribution correction (matches gpt-5 fix from v0.6.1 in shape)
# ---------------------------------------------------------------------------


def test_openai_o3_pricing_corrected():
    """v0.6.2 fix: o3 was (10.00, 40.00) speculative in v0.6.1; real
    direct OpenAI pricing per the authoritative page is (2.00, 8.00).
    5x over-attribution window from v0.5.x through v0.6.1. Same class
    as the gpt-5 fix in v0.6.1."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["o3"] == (2.00, 8.00), (
        "o3 PRICING must reflect direct OpenAI pricing, NOT v0.6.1 "
        "speculative (10.00, 40.00)"
    )


def test_openai_o1_mini_pricing_corrected():
    """v0.6.2 fix: o1-mini was (3.00, 12.00) in v0.6.1; real is (1.10,
    4.40) per authoritative page (matches o3-mini + o4-mini tier)."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["o1-mini"] == (1.10, 4.40)


def test_openai_pricing_v0_6_2_metadata():
    from swarph_mesh.adapters.openai import (
        _OPENAI_PRICING_VERIFIED_AT,
        _OPENAI_PRICING_SOURCE,
    )

    assert _OPENAI_PRICING_VERIFIED_AT == "2026-05-09"
    assert "openai.com" in _OPENAI_PRICING_SOURCE


# ---------------------------------------------------------------------------
# Centralized normalizers (drop DM #745 obs #1)
# ---------------------------------------------------------------------------


def test_normalize_xai_id_centralized():
    """Top-level discovery.normalize_xai_id matches adapter-local
    semantics."""
    from swarph_mesh import normalize_xai_id

    assert normalize_xai_id("x-ai/grok-4-07-09") == "grok-4"
    assert normalize_xai_id("x-ai/grok-3-beta") == "grok-3"
    assert normalize_xai_id("grok-4-fast-reasoning") == "grok-4-fast-reasoning"


def test_normalize_deepseek_id_centralized():
    from swarph_mesh import normalize_deepseek_id

    assert normalize_deepseek_id("deepseek/deepseek-chat-v3.1") == "deepseek-chat"
    assert (
        normalize_deepseek_id("deepseek/deepseek-reasoner-v3.1-terminus")
        == "deepseek-reasoner"
    )
    assert normalize_deepseek_id("deepseek-v4-flash") == "deepseek-v4-flash"


def test_normalize_model_id_dispatches_by_provider():
    """Provider-aware shared dispatcher."""
    from swarph_mesh import normalize_model_id

    assert normalize_model_id("grok", "x-ai/grok-4") == "grok-4"
    assert normalize_model_id("xai", "x-ai/grok-3-beta") == "grok-3"
    assert normalize_model_id("deepseek", "deepseek/deepseek-chat-v3.1") == "deepseek-chat"
    # Pass-through for providers without prefix conventions
    assert normalize_model_id("openai", "gpt-5") == "gpt-5"
    assert normalize_model_id("claude", "claude-opus-4-7") == "claude-opus-4-7"


def test_centralized_normalizers_match_adapter_local():
    """Sanity: centralized helpers return same result as the
    adapter-local copies (which stay for back-compat)."""
    from swarph_mesh import normalize_xai_id, normalize_deepseek_id
    from swarph_mesh.adapters.grok import _normalize_xai_id
    from swarph_mesh.adapters.deepseek import _normalize_deepseek_id

    test_inputs = [
        ("x-ai/grok-4", "grok"),
        ("x-ai/grok-4-07-09", "grok"),
        ("x-ai/grok-3-beta", "grok"),
        ("grok-4", "grok"),
    ]
    for model_id, _ in test_inputs:
        assert normalize_xai_id(model_id) == _normalize_xai_id(model_id)

    deepseek_inputs = [
        "deepseek/deepseek-chat-v3.1",
        "deepseek/deepseek-reasoner-v3.1-terminus",
        "deepseek-v4-flash",
    ]
    for model_id in deepseek_inputs:
        assert normalize_deepseek_id(model_id) == _normalize_deepseek_id(model_id)


# ---------------------------------------------------------------------------
# Retirement registry (drop DM #745 obs #2)
# ---------------------------------------------------------------------------


def test_retirement_registry_grok_4_pre_retirement():
    """grok-4 retires 2026-05-15. Today (2026-05-09) is pre-retirement."""
    from swarph_mesh import is_retired

    today = datetime.date(2026, 5, 9)
    assert not is_retired("grok", "grok-4", today=today)


def test_retirement_registry_grok_4_post_retirement():
    """After 2026-05-15, grok-4 is retired."""
    from swarph_mesh import is_retired

    post_retirement = datetime.date(2026, 5, 16)
    assert is_retired("grok", "grok-4", today=post_retirement)


def test_retirement_registry_unknown_model_not_retired():
    from swarph_mesh import is_retired

    assert not is_retired("openai", "gpt-4o")
    assert not is_retired("claude", "claude-opus-4-7")


def test_retirement_registry_deprecated_sentinel_not_retired():
    """Models marked 'deprecated' (e.g., claude-sonnet-3-7) are still
    routable — deprecated != retired."""
    from swarph_mesh import is_retired

    assert not is_retired("claude", "claude-sonnet-3-7")
    assert not is_retired("claude", "claude-opus-3")


def test_retirement_date_lookup():
    from swarph_mesh import retirement_date

    assert retirement_date("grok", "grok-4") == "2026-05-15"
    assert retirement_date("grok", "grok-code-fast-1") == "2026-05-15"
    assert retirement_date("claude", "claude-sonnet-3-7") == "deprecated"
    assert retirement_date("openai", "gpt-4o") is None  # not in registry


def test_retirement_date_isoformat_parseable():
    """Dated entries (not 'deprecated' sentinel) must be ISO-parseable."""
    from swarph_mesh.discovery import _RETIREMENT_REGISTRY

    for key, value in _RETIREMENT_REGISTRY.items():
        if value == "deprecated":
            continue
        # Should not raise
        datetime.date.fromisoformat(value)
