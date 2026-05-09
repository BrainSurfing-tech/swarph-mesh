"""Tests for ``swarph_mesh.discovery`` — mocked HTTP + cache + filter logic.

Live smoke against ``https://api.aimlapi.com/models`` lives in
``test_smoke_discovery.py`` (gated on network reachability).
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

import pytest

from swarph_mesh.discovery import (
    AIMLAPI_MODELS_URL,
    ModelInfo,
    _fetch_aimlapi_catalog,
    _is_cache_fresh,
    _parse_aimlapi_entry,
    _PROVIDER_TO_DEVELOPER,
    get_model_info,
    invalidate_catalog,
    is_model_supported,
    list_models,
)


# ---------------------------------------------------------------------------
# ModelInfo + parser
# ---------------------------------------------------------------------------


def test_modelinfo_matches_developer_normalizes_spacing():
    m = ModelInfo(id="x", developer="Open AI")
    assert m.matches_developer("OpenAI")
    assert m.matches_developer("open ai")
    assert m.matches_developer("Open AI")


def test_modelinfo_matches_developer_handles_hyphens():
    m = ModelInfo(id="x", developer="X AI")
    assert m.matches_developer("xai")
    assert m.matches_developer("X-AI")


def test_modelinfo_matches_developer_rejects_mismatch():
    m = ModelInfo(id="x", developer="Open AI")
    assert not m.matches_developer("Anthropic")
    assert not m.matches_developer("Google")


def test_parse_aimlapi_entry_happy_path():
    entry = {
        "id": "gpt-4o",
        "info": {
            "name": "GPT 4o",
            "developer": "Open AI",
            "contextLength": 128000,
            "maxTokens": 16384,
        },
        "type": "openai/chat-completions",
        "aliases": ["openai/gpt-4o"],
        "tags": ["playground:chat"],
    }
    m = _parse_aimlapi_entry(entry)
    assert m is not None
    assert m.id == "gpt-4o"
    assert m.developer == "Open AI"
    assert m.context_length == 128000
    assert m.max_tokens == 16384
    assert m.aliases == ["openai/gpt-4o"]
    assert m.source == "aimlapi"


def test_parse_aimlapi_entry_missing_id_returns_none():
    assert _parse_aimlapi_entry({"info": {"developer": "x"}}) is None


def test_parse_aimlapi_entry_missing_optional_fields_uses_defaults():
    m = _parse_aimlapi_entry({"id": "minimal", "info": {"developer": "x"}})
    assert m is not None
    assert m.id == "minimal"
    assert m.context_length is None
    assert m.aliases == []


# ---------------------------------------------------------------------------
# Catalog fetch (mocked HTTP) + cache + dedup
# ---------------------------------------------------------------------------


_FAKE_CATALOG = {
    "object": "list",
    "data": [
        {"id": "gpt-4o", "info": {"developer": "Open AI", "contextLength": 128000},
         "type": "openai/chat-completions", "aliases": [], "tags": []},
        {"id": "gpt-4o", "info": {"developer": "Open AI", "contextLength": 128000},  # dup
         "type": "openai/chat-completions", "aliases": [], "tags": []},
        {"id": "claude-opus-4-7", "info": {"developer": "Anthropic", "contextLength": 1000000},
         "type": "anthropic/chat-completions", "aliases": [], "tags": []},
        {"id": "grok-4", "info": {"developer": "X AI", "contextLength": 256000},
         "type": "xai/chat-completions", "aliases": ["x-ai/grok-4"], "tags": []},
        {"id": "deepseek-v4-flash", "info": {"developer": "DeepSeek AI", "contextLength": 1000000},
         "type": "deepseek/chat-completions", "aliases": [], "tags": []},
    ],
}


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a fresh discovery cache."""
    invalidate_catalog()
    yield
    invalidate_catalog()


def _mock_urlopen_factory(payload):
    """Returns a mock for ``urllib.request.urlopen`` that returns
    ``payload`` JSON-encoded."""
    class _MockResponse:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        return _MockResponse(json.dumps(payload).encode("utf-8"))

    return _open


def test_fetch_aimlapi_catalog_dedupes_by_id(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    out = _fetch_aimlapi_catalog()
    ids = [m.id for m in out]
    assert "gpt-4o" in ids
    assert ids.count("gpt-4o") == 1  # deduped


def test_fetch_aimlapi_catalog_includes_all_developers(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    out = _fetch_aimlapi_catalog()
    devs = {m.developer for m in out}
    assert {"Open AI", "Anthropic", "X AI", "DeepSeek AI"}.issubset(devs)


def test_list_models_no_provider_returns_full_catalog(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    out = list_models()
    # 5 entries in fake catalog, 1 dup → 4 unique
    assert len(out) == 4


def test_list_models_filters_by_provider(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    openai_models = list_models(provider="openai")
    assert len(openai_models) == 1
    assert openai_models[0].id == "gpt-4o"

    anthropic_models = list_models(provider="claude")
    assert len(anthropic_models) == 1
    assert anthropic_models[0].id == "claude-opus-4-7"

    grok_models = list_models(provider="grok")
    assert len(grok_models) == 1
    assert grok_models[0].id == "grok-4"

    deepseek_models = list_models(provider="deepseek")
    assert len(deepseek_models) == 1


def test_list_models_provider_alias_resolution(monkeypatch):
    """``provider="claude"`` and ``provider="anthropic"`` both resolve
    to AIMLAPI's ``Anthropic`` developer."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    a = list_models(provider="claude")
    b = list_models(provider="anthropic")
    assert {m.id for m in a} == {m.id for m in b}


def test_list_models_caches_within_ttl(monkeypatch):
    """Within TTL, second call should not refetch."""
    call_count = [0]

    def _counting_open(req, timeout=None):
        call_count[0] += 1

        class _R:
            def read(self):
                return json.dumps(_FAKE_CATALOG).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _counting_open)
    list_models()
    list_models()
    list_models(provider="openai")
    assert call_count[0] == 1  # only first call hit the network


def test_list_models_ttl_zero_forces_refresh(monkeypatch):
    call_count = [0]

    def _counting_open(req, timeout=None):
        call_count[0] += 1

        class _R:
            def read(self):
                return json.dumps(_FAKE_CATALOG).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setattr("urllib.request.urlopen", _counting_open)
    list_models()
    list_models(ttl_seconds=0)  # force-refresh
    assert call_count[0] == 2


def test_invalidate_catalog_clears_cache(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    list_models()
    assert _is_cache_fresh(86400)
    invalidate_catalog()
    assert not _is_cache_fresh(86400)


# ---------------------------------------------------------------------------
# Network failure → fallback
# ---------------------------------------------------------------------------


def test_aimlapi_unreachable_falls_back_gracefully(monkeypatch, capsys):
    """When AIMLAPI fetch raises URLError, list_models triggers the
    per-provider fallback. The exact contents depend on which provider
    keys are configured; we only assert no crash + a list result."""

    def _failing_open(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _failing_open)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    # DeepSeek fallback also tries the legacy file. Mock that to None
    # so we get a deterministic empty result on lab-OVH where the
    # legacy file is present.
    import swarph_mesh.adapters.deepseek as _ds

    monkeypatch.setattr(_ds, "_resolve_api_key", lambda: None)

    out = list_models()
    assert isinstance(out, list)
    assert out == []  # graceful empty (no provider keys → no fallback data)


def test_aimlapi_unreachable_with_openai_key_uses_fallback(monkeypatch):
    """When AIMLAPI is down but OPENAI_API_KEY is set, the OpenAI
    fallback path runs via openai.OpenAI client."""

    def _failing_open(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", _failing_open)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-tok")

    # Mock the openai SDK
    class _MockModel:
        def __init__(self, id):
            self.id = id
            self.object = "model"

    class _MockModels:
        def list(self):
            class _R:
                data = [_MockModel("gpt-4o"), _MockModel("o1")]

            return _R()

    class _MockClient:
        def __init__(self, **kwargs):
            self.models = _MockModels()

    with patch("openai.OpenAI", _MockClient):
        out = list_models(provider="openai")

    assert {m.id for m in out} == {"gpt-4o", "o1"}
    assert all(m.source == "openai" for m in out)


# ---------------------------------------------------------------------------
# is_model_supported + get_model_info
# ---------------------------------------------------------------------------


def test_is_model_supported_true_for_known(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    assert is_model_supported("gpt-4o")
    assert is_model_supported("claude-opus-4-7")


def test_is_model_supported_false_for_unknown(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    assert not is_model_supported("definitely-not-a-real-model-2099")


def test_is_model_supported_matches_aliases(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    # grok-4 has alias "x-ai/grok-4"
    assert is_model_supported("x-ai/grok-4")


def test_get_model_info_returns_full_record(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    m = get_model_info("claude-opus-4-7")
    assert m is not None
    assert m.context_length == 1000000


def test_get_model_info_returns_none_for_unknown(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    assert get_model_info("nope") is None


# ---------------------------------------------------------------------------
# Adapter Protocol-level integration — every adapter has list_models
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pricing — Anthropic manual table
# ---------------------------------------------------------------------------


def test_pricing_for_anthropic_opus_4_7():
    from swarph_mesh.discovery import pricing_for_anthropic_model

    p = pricing_for_anthropic_model("claude-opus-4-7")
    assert p is not None
    assert p.input_per_mtok == 5.00
    assert p.output_per_mtok == 25.00
    assert p.usage_unit == "MTok"
    assert p.source == "anthropic-docs-manual"
    assert p.verified_at  # ISO date string


def test_pricing_for_anthropic_carries_cache_dimensions():
    """Cache write/hit pricing is stashed on the record as attributes."""
    from swarph_mesh.discovery import pricing_for_anthropic_model

    p = pricing_for_anthropic_model("claude-opus-4-7")
    assert p is not None
    # 5m cache write = 1.25x base input (5 → 6.25)
    assert p.cache_write_5m_per_mtok == 6.25
    # 1h cache write = 2x base (5 → 10)
    assert p.cache_write_1h_per_mtok == 10.00
    # cache hit = 0.1x base (5 → 0.50)
    assert p.cache_hit_per_mtok == 0.50


def test_pricing_for_anthropic_unknown_returns_none():
    from swarph_mesh.discovery import pricing_for_anthropic_model

    assert pricing_for_anthropic_model("claude-future-2099") is None


def test_anthropic_pricing_covers_all_modern_models():
    """Spot-check that the manual table includes every model our claude
    adapter's PRICING dict references — drift would surface here first."""
    from swarph_mesh.adapters.claude import PRICING as adapter_pricing
    from swarph_mesh.discovery import pricing_for_anthropic_model

    for model_id in adapter_pricing:
        if model_id == "_default":
            continue
        p = pricing_for_anthropic_model(model_id)
        assert p is not None, f"adapter PRICING has {model_id!r} but discovery doesn't"


def test_list_anthropic_pricing_returns_all_records():
    from swarph_mesh.discovery import _ANTHROPIC_PRICING, list_anthropic_pricing

    records = list_anthropic_pricing()
    assert len(records) == len(_ANTHROPIC_PRICING)
    # Every record has the documented shape
    for r in records:
        assert r.provider == "anthropic"
        assert r.input_per_mtok > 0
        assert r.output_per_mtok > 0
        assert r.usage_unit == "MTok"


# ---------------------------------------------------------------------------
# Pricing — Gemini Cloud Billing API
# ---------------------------------------------------------------------------


def test_money_to_usd_combines_units_and_nanos():
    from swarph_mesh.discovery import _money_to_usd

    # $10.50 = 10 units + 500_000_000 nanos
    assert _money_to_usd("10", 500_000_000) == 10.5
    # $0.000035 = 0 units + 35_000 nanos
    assert _money_to_usd(0, 35_000) == 0.000035
    # Empty/None → 0
    assert _money_to_usd(None, None) == 0.0


def test_classify_gemini_sku_input_base_tier():
    from swarph_mesh.discovery import _classify_gemini_sku

    direction, tier = _classify_gemini_sku("Gemini 1.5 Pro Input Tokens")
    assert direction == "input"
    assert tier == 0


def test_classify_gemini_sku_output_long_context():
    from swarph_mesh.discovery import _classify_gemini_sku

    direction, tier = _classify_gemini_sku(
        "Gemini 1.5 Pro Output Tokens (Greater than 128k)"
    )
    assert direction == "output"
    assert tier == 128000


def test_classify_gemini_sku_unrecognized_returns_none():
    from swarph_mesh.discovery import _classify_gemini_sku

    direction, tier = _classify_gemini_sku("Gemini Vector Store Storage")
    assert direction is None


def test_fetch_gemini_pricing_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_BILLING_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)
    from swarph_mesh.discovery import fetch_gemini_pricing, invalidate_pricing

    invalidate_pricing("gemini")
    out = fetch_gemini_pricing()
    assert out == []


def test_fetch_gemini_pricing_parses_mocked_response(monkeypatch):
    """Mock Cloud Billing API response shape and verify parsing."""
    fake_skus = {
        "skus": [
            {
                "skuId": "ABCD-1234",
                "description": "Gemini 1.5 Pro Input Tokens",
                "pricingInfo": [
                    {
                        "pricingExpression": {
                            "usageUnit": "1MCT",
                            "tieredRates": [
                                {
                                    "startUsageAmount": 0,
                                    "unitPrice": {
                                        "currencyCode": "USD",
                                        "units": "1",
                                        "nanos": 250_000_000,  # $1.25
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
            {
                "skuId": "EFGH-5678",
                "description": "Gemini 1.5 Pro Output Tokens",
                "pricingInfo": [
                    {
                        "pricingExpression": {
                            "usageUnit": "1MCT",
                            "tieredRates": [
                                {
                                    "startUsageAmount": 0,
                                    "unitPrice": {
                                        "currencyCode": "USD",
                                        "units": "5",
                                        "nanos": 0,  # $5.00
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
            # Non-Gemini SKU should be skipped
            {
                "skuId": "OTHER-9999",
                "description": "Some other Vertex AI service",
                "pricingInfo": [
                    {"pricingExpression": {"tieredRates": []}}
                ],
            },
        ]
    }

    def _mock_open(req, timeout=None):
        class _R:
            def read(self):
                return json.dumps(fake_skus).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("GOOGLE_CLOUD_BILLING_API_KEY", "fake")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)
    from swarph_mesh.discovery import fetch_gemini_pricing, invalidate_pricing

    invalidate_pricing("gemini")
    out = fetch_gemini_pricing()
    # Two Gemini SKUs (input + output), the third is filtered
    assert len(out) == 2
    inputs = [r for r in out if r.input_per_mtok is not None]
    outputs = [r for r in out if r.output_per_mtok is not None]
    assert len(inputs) == 1
    assert len(outputs) == 1
    assert inputs[0].input_per_mtok == 1.25
    assert outputs[0].output_per_mtok == 5.0
    # Source + verified_at populated
    assert inputs[0].source == "google-cloud-billing"
    assert inputs[0].verified_at  # ISO timestamp


def test_fetch_gemini_pricing_caches_within_ttl(monkeypatch):
    call_count = [0]

    def _mock_open(req, timeout=None):
        call_count[0] += 1

        class _R:
            def read(self):
                return json.dumps({"skus": []}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("GOOGLE_CLOUD_BILLING_API_KEY", "fake")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)
    from swarph_mesh.discovery import fetch_gemini_pricing, invalidate_pricing

    invalidate_pricing("gemini")
    fetch_gemini_pricing()
    fetch_gemini_pricing()
    fetch_gemini_pricing()
    assert call_count[0] == 1  # cached after first


def test_pricing_for_gemini_model_finds_match(monkeypatch):
    fake_skus = {
        "skus": [
            {
                "skuId": "ABCD-1234",
                "description": "Gemini 1.5 Pro Output Tokens",
                "pricingInfo": [
                    {
                        "pricingExpression": {
                            "usageUnit": "1MCT",
                            "tieredRates": [
                                {
                                    "startUsageAmount": 0,
                                    "unitPrice": {
                                        "units": "5",
                                        "nanos": 0,
                                    },
                                }
                            ],
                        }
                    }
                ],
            }
        ]
    }

    def _mock_open(req, timeout=None):
        class _R:
            def read(self):
                return json.dumps(fake_skus).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("GOOGLE_CLOUD_BILLING_API_KEY", "fake")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)
    from swarph_mesh.discovery import (
        invalidate_pricing,
        pricing_for_gemini_model,
    )

    invalidate_pricing("gemini")
    price = pricing_for_gemini_model("1.5 Pro", direction="output")
    assert price == 5.0


def test_pricing_for_gemini_model_returns_none_no_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_BILLING_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_API_KEY", raising=False)
    from swarph_mesh.discovery import (
        invalidate_pricing,
        pricing_for_gemini_model,
    )

    invalidate_pricing("gemini")
    assert pricing_for_gemini_model("1.5 Pro") is None


# ---------------------------------------------------------------------------
# Cost reconciliation — OpenAI /v1/organization/costs (admin-key gated)
# ---------------------------------------------------------------------------

_FAKE_COST_RESPONSE = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "start_time": 1730419200,
            "end_time": 1730505600,
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.06, "currency": "usd"},
                    "line_item": None,
                    "project_id": None,
                    "api_key_id": None,
                }
            ],
        },
        {
            "object": "bucket",
            "start_time": 1730505600,
            "end_time": 1730592000,
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.12, "currency": "usd"},
                    "line_item": "gpt-4o input",
                    "project_id": "proj_swarph",
                    "api_key_id": None,
                },
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.05, "currency": "usd"},
                    "line_item": "gpt-4o output",
                    "project_id": "proj_swarph",
                    "api_key_id": None,
                },
            ],
        },
    ],
    "has_more": False,
    "next_page": None,
}


def test_resolve_openai_admin_key_arg_wins(monkeypatch):
    from swarph_mesh.discovery import _resolve_openai_admin_key

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "from-env")
    assert _resolve_openai_admin_key("from-arg") == "from-arg"


def test_resolve_openai_admin_key_env_fallback(monkeypatch):
    from swarph_mesh.discovery import _resolve_openai_admin_key

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "from-env")
    assert _resolve_openai_admin_key(None) == "from-env"


def test_resolve_openai_admin_key_none_when_unset(monkeypatch):
    from swarph_mesh.discovery import _resolve_openai_admin_key

    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    assert _resolve_openai_admin_key(None) is None


def test_parse_cost_amount_value_shape():
    from swarph_mesh.discovery import _parse_cost_amount

    usd, currency = _parse_cost_amount({"value": 0.06, "currency": "usd"})
    assert usd == 0.06
    assert currency == "usd"


def test_parse_cost_amount_units_nanos_fallback():
    """When `value` field is absent, fall back to the units+nanos shape."""
    from swarph_mesh.discovery import _parse_cost_amount

    usd, currency = _parse_cost_amount({"units": "10", "nanos": 500_000_000})
    assert usd == 10.5


def test_parse_cost_amount_empty():
    from swarph_mesh.discovery import _parse_cost_amount

    usd, currency = _parse_cost_amount(None)
    assert usd == 0.0


def test_fetch_openai_cost_buckets_returns_empty_no_key(monkeypatch):
    from swarph_mesh.discovery import fetch_openai_cost_buckets

    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    out = fetch_openai_cost_buckets(start_time=1730419200)
    assert out == []


def test_fetch_openai_cost_buckets_parses_mocked(monkeypatch):
    from swarph_mesh.discovery import fetch_openai_cost_buckets

    def _mock_open(req, timeout=None):
        # Verify auth header is sent
        assert req.headers.get("Authorization") == "Bearer fake-admin-key"
        assert "/v1/organization/costs" in req.full_url
        assert "start_time=1730419200" in req.full_url

        class _R:
            def read(self):
                return json.dumps(_FAKE_COST_RESPONSE).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "fake-admin-key")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    buckets = fetch_openai_cost_buckets(start_time=1730419200)
    assert len(buckets) == 2
    # Buckets sorted by start_time
    assert buckets[0].start_time == 1730419200
    assert buckets[0].total_usd == 0.06
    assert buckets[1].total_usd == pytest.approx(0.17)
    # Line-item breakdown populated for second bucket
    assert "gpt-4o input" in buckets[1].line_item_breakdown
    assert buckets[1].line_item_breakdown["gpt-4o input"] == 0.12
    assert buckets[1].project_breakdown.get("proj_swarph") == pytest.approx(0.17)


def test_reconcile_openai_cost_computes_drift(monkeypatch):
    from swarph_mesh.discovery import reconcile_openai_cost

    def _mock_open(req, timeout=None):
        class _R:
            def read(self):
                return json.dumps(_FAKE_COST_RESPONSE).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "fake")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    # OpenAI says we spent 0.06 + 0.17 = 0.23
    # We claim we attributed 0.20 → drift +0.03 = +15%
    result = reconcile_openai_cost(
        start_time=1730419200, swarph_attributed_usd=0.20
    )
    assert result["openai_actual_usd"] == pytest.approx(0.23)
    assert result["swarph_attributed_usd"] == 0.20
    assert result["drift_usd"] == pytest.approx(0.03)
    assert result["drift_pct"] == pytest.approx(15.0)
    assert len(result["buckets"]) == 2


def test_reconcile_openai_cost_no_attribution_skips_drift(monkeypatch):
    from swarph_mesh.discovery import reconcile_openai_cost

    def _mock_open(req, timeout=None):
        class _R:
            def read(self):
                return json.dumps(_FAKE_COST_RESPONSE).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "fake")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    result = reconcile_openai_cost(start_time=1730419200)
    assert result["openai_actual_usd"] == pytest.approx(0.23)
    assert result["swarph_attributed_usd"] is None
    assert result["drift_usd"] is None
    assert result["drift_pct"] is None


def test_fetch_openai_cost_buckets_handles_4xx_gracefully(monkeypatch):
    """403 (e.g. admin key revoked) returns [] not crash."""
    from swarph_mesh.discovery import fetch_openai_cost_buckets

    def _mock_open(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, None
        )

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "fake")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    out = fetch_openai_cost_buckets(start_time=1730419200)
    assert out == []


# ---------------------------------------------------------------------------
# Existing test
# ---------------------------------------------------------------------------


def test_all_five_adapters_expose_list_models(monkeypatch):
    """Every adapter shipped today implements LLMAdapter Protocol's
    new list_models() method (v0.6.0 architectural promotion)."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _mock_urlopen_factory(_FAKE_CATALOG),
    )
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("XAI_API_KEY", "fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")

    from swarph_mesh.adapters.openai import OpenAIAdapter
    from swarph_mesh.adapters.grok import GrokAdapter
    from swarph_mesh.adapters.deepseek import DeepSeekAdapter

    # Adapters that don't need filesystem (gemini/claude need bridge or
    # claude binary; skip those here — covered by unit tests in their
    # own files).
    for cls in (OpenAIAdapter, GrokAdapter, DeepSeekAdapter):
        a = cls(api_key="fake")
        result = a.list_models()
        assert isinstance(result, list)
        # Each adapter filters to its own developer
        for m in result:
            assert m.matches_developer(_PROVIDER_TO_DEVELOPER[a.name])
