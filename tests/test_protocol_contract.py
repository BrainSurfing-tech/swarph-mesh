"""Protocol-stability contract tests.

The graph framing (commander 2026-05-09): every CLI is a node in the
graph, and the ``LLMAdapter`` Protocol IS the contract third-party
node implementers depend on. Breaking changes to this Protocol orphan
external implementations.

This test file is a **frozen snapshot** of the Protocol surface. Any
change here that's not paired with a deliberate version bump + entry
in ``DEPRECATIONS.md`` (when that lands) is a contract regression.

If a test in this file fails because of a Protocol change, the right
move is:

1. Decide if this is a v0.x.y patch (NOT allowed for Protocol changes
   — see semver policy)
2. Or a v0.X.0 minor with deprecation period (preferred)
3. Or a vX.0.0 major with migration guide

Then update the snapshot here AND log the change in ``DEPRECATIONS.md``
so external node implementers see it.

The frozen-as-of marker is part of the assertions so a manual eyeball
read of the test file is enough to know what version's contract is
locked in.
"""

from __future__ import annotations

import inspect
import typing

from swarph_mesh.types import (
    ChatMessage,
    LLMAdapter,
    LLMResponse,
)


# ---------------------------------------------------------------------------
# Frozen-as-of marker
# ---------------------------------------------------------------------------

PROTOCOL_FROZEN_AT_VERSION = "0.7.0"


# ---------------------------------------------------------------------------
# LLMAdapter Protocol — the third-party node interface contract
# ---------------------------------------------------------------------------


def test_llm_adapter_is_runtime_checkable():
    """Third-party node implementers rely on isinstance() to verify
    Protocol satisfaction at construction. Removing
    @runtime_checkable would silently break that workflow."""
    # The marker for runtime_checkable Protocols is a __runtime_checkable__
    # attribute on the class (typing module sets this).
    assert hasattr(LLMAdapter, "_is_runtime_protocol")
    assert LLMAdapter._is_runtime_protocol is True


def test_llm_adapter_has_required_class_attributes():
    """``name`` and ``default_model`` are class-level attributes on
    every conforming adapter. v0.6.0+ contract."""
    annotations = typing.get_type_hints(LLMAdapter)
    assert "name" in annotations, "LLMAdapter must declare class-level `name: str`"
    assert "default_model" in annotations, (
        "LLMAdapter must declare class-level `default_model: str`"
    )


def test_llm_adapter_method_set():
    """Frozen method set of LLMAdapter as of PROTOCOL_FROZEN_AT_VERSION.
    Adding to this set requires a minor version bump + entry in
    DEPRECATIONS.md. Removing requires a major bump.

    Renaming a method — ALWAYS a breaking change requiring deprecation
    period. The Protocol is the third-party node-implementer contract;
    silent renames orphan external implementations.
    """
    expected_methods = {"chat", "stream", "cost_per_token", "list_models"}
    actual_methods = {
        name
        for name in dir(LLMAdapter)
        if not name.startswith("_")
        and callable(getattr(LLMAdapter, name, None))
    }
    # Filter out the dataclass-style attributes that aren't methods
    actual_methods -= {"name", "default_model"}

    assert actual_methods == expected_methods, (
        f"LLMAdapter method set drift detected!\n"
        f"  Expected (frozen at v{PROTOCOL_FROZEN_AT_VERSION}): {sorted(expected_methods)}\n"
        f"  Actual: {sorted(actual_methods)}\n"
        f"  Diff: added={sorted(actual_methods - expected_methods)} "
        f"removed={sorted(expected_methods - actual_methods)}\n"
        f"  If intentional, update PROTOCOL_FROZEN_AT_VERSION + add entry "
        f"to DEPRECATIONS.md."
    )


def test_chat_signature_frozen():
    """``chat(messages, model, system_prompt=None, json_schema=None,
    temperature=0.7, max_tokens=None) -> LLMResponse`` is the v0.6+
    signature. External adapter implementations rely on this exact
    keyword-argument shape."""
    sig = inspect.signature(LLMAdapter.chat)
    params = sig.parameters

    # First param is `self` (always)
    assert "self" in params

    # Required positional/keyword params
    assert "messages" in params
    assert "model" in params

    # Keyword params with defaults — third-party SwarphCall callers
    # rely on these being keyword-default so they can omit them.
    expected_kwarg_defaults = {
        "system_prompt": None,
        "json_schema": None,
        "temperature": 0.7,
        "max_tokens": None,
    }
    for kwarg, default in expected_kwarg_defaults.items():
        assert kwarg in params, f"chat() missing required kwarg: {kwarg}"
        assert params[kwarg].default == default, (
            f"chat() kwarg {kwarg!r} default changed: "
            f"expected {default!r}, got {params[kwarg].default!r}"
        )


def test_cost_per_token_signature_frozen():
    """``cost_per_token(model: str) -> tuple[float, float]`` returns
    ``(input_per_mtok, output_per_mtok)``. Used by attribution writers
    + cost reconciliation primitives."""
    sig = inspect.signature(LLMAdapter.cost_per_token)
    assert "self" in sig.parameters
    assert "model" in sig.parameters


def test_list_models_signature_frozen():
    """v0.6.0 architectural promotion. Default ``ttl_seconds=86400``
    (24h cache). Returns ``list[ModelInfo]`` from the centralized
    discovery primitive."""
    sig = inspect.signature(LLMAdapter.list_models)
    params = sig.parameters
    assert "self" in params
    assert "ttl_seconds" in params
    assert params["ttl_seconds"].default == 86400, (
        f"list_models() ttl_seconds default changed: "
        f"expected 86400 (24h), got {params['ttl_seconds'].default!r}"
    )


# ---------------------------------------------------------------------------
# ChatMessage + LLMResponse — public dataclass contracts
# ---------------------------------------------------------------------------


def test_chatmessage_field_set():
    """``ChatMessage`` is the canonical input shape callers send to
    SwarphCall.chat(). Frozen field set."""
    expected = {"role", "content"}
    actual = set(ChatMessage.model_fields.keys())
    assert expected.issubset(actual), (
        f"ChatMessage missing required fields. "
        f"Expected at least {expected}, got {actual}"
    )


def test_llmresponse_field_set():
    """``LLMResponse`` is the canonical output shape SwarphCall.chat()
    returns. Frozen field set as of PROTOCOL_FROZEN_AT_VERSION.

    Adding fields with defaults is OK (non-breaking); removing OR
    making optional fields required is a breaking change.
    """
    expected_required_or_optional = {
        "text",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "duration_s",
        "cached",
        "raw_response",
        "error_class",
        "parsed",
    }
    actual = set(LLMResponse.model_fields.keys())
    missing = expected_required_or_optional - actual
    assert not missing, (
        f"LLMResponse missing fields from frozen contract: {missing}"
    )


# ---------------------------------------------------------------------------
# Adapter-name canonical set — graph-wide identity guarantee
# ---------------------------------------------------------------------------


def test_canonical_adapter_names_in_registry_dispatch():
    """Each canonical adapter name is dispatchable through the
    registry. Third-party plugins picking provider names must not
    collide with these reserved names: gemini, deepseek, claude,
    openai, grok.

    Renaming an adapter (e.g., gemini → google) would orphan every
    consumer pinning by string name. Always-add-never-rename
    discipline.

    NOTE: ``get_adapter`` caches singletons per provider. Constructing
    with a fake key here would poison the registry for any later
    live-smoke test that needs a real key. We use ``reset_registry()``
    in a try/finally so registry state is restored even on assertion
    failure.
    """
    from swarph_mesh.adapters import get_adapter, reset_registry
    from swarph_mesh.exceptions import UnknownProvider

    canonical_names = {"gemini", "deepseek", "claude", "openai", "grok"}

    # Snapshot any registry contents that existed pre-test so we can
    # restore. reset_registry() clears everything; tests later in the
    # same suite that depend on real-key singletons must build them
    # fresh post-reset, which they do via ``get_adapter`` lazy init.
    reset_registry()
    try:
        for name in canonical_names:
            try:
                adapter = get_adapter(name, api_key="fake-for-construction-only")
            except UnknownProvider:
                raise AssertionError(
                    f"canonical adapter name {name!r} is not registered in "
                    f"swarph_mesh.adapters dispatch. The graph contract "
                    f"reserves these five names; if you intentionally "
                    f"removed one, that is a breaking change requiring a "
                    f"DEPRECATIONS.md entry + major version bump."
                )
            assert adapter.name == name, (
                f"Adapter for {name!r} reports its own name as "
                f"{adapter.name!r} — name divergence from registry key. "
                f"This will silently route attribution rows to the wrong "
                f"provider name; fix the adapter's `name` class-attribute."
            )
    finally:
        # Always reset so subsequent live-smoke tests construct fresh
        # adapters with real keys from env, not our fake placeholder.
        reset_registry()


# ---------------------------------------------------------------------------
# Discovery primitives — graph-wide contract
# ---------------------------------------------------------------------------


def test_discovery_public_api_frozen():
    """The discovery module's public API is part of the graph contract.
    Third-party tools (drift-detection cron, dashboards, custom
    cost-reconciliation pipelines) import these symbols. Frozen as of
    v0.6.2."""
    from swarph_mesh import discovery

    expected_public_api = {
        # v0.6.0 catalog
        "ModelInfo",
        "list_models",
        "is_model_supported",
        "get_model_info",
        "invalidate_catalog",
        # v0.6.0 pricing primitives
        "ProviderPricing",
        "fetch_gemini_pricing",
        "pricing_for_gemini_model",
        "pricing_for_anthropic_model",
        "list_anthropic_pricing",
        "invalidate_pricing",
        # v0.6.0 cost reconciliation
        "CostBucket",
        "fetch_openai_cost_buckets",
        "reconcile_openai_cost",
        # v0.6.1 cost reconciliation + balance
        "XAICostBucket",
        "fetch_xai_cost_buckets",
        "reconcile_xai_cost",
        "DeepSeekBalance",
        "fetch_deepseek_balance",
        # v0.6.2 normalizers + retirement registry
        "normalize_xai_id",
        "normalize_deepseek_id",
        "normalize_model_id",
        "is_retired",
        "retirement_date",
    }
    actual_public = {
        name
        for name in dir(discovery)
        if not name.startswith("_") and callable(getattr(discovery, name))
        or (not name.startswith("_") and name in expected_public_api)
    }
    missing = expected_public_api - actual_public
    assert not missing, (
        f"discovery public API missing symbols from frozen contract: "
        f"{missing}. If intentional, update PROTOCOL_FROZEN_AT_VERSION + "
        f"DEPRECATIONS.md."
    )
