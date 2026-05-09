"""v0.6.1 PRICING-table catch-up tests.

Three concerns covered:

1. gpt-5 OVER-ATTRIBUTION FIX — was (5.00, 20.00) speculative; real direct
   OpenAI pricing is (1.25, 10.00). Regression test asserts the corrected
   numbers.
2. New PRICING entries (gpt-4.1 family, gpt-5-mini/nano, claude-opus-4-1,
   grok-4-fast, etc.) — assert they exist with documented prices.
3. Alias-resolution helpers — ``_normalize_xai_id`` and
   ``_normalize_deepseek_id`` map prefixed/dated AIMLAPI catalog IDs to
   canonical PRICING entries.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# OpenAI: gpt-5 over-attribution fix + new entries
# ---------------------------------------------------------------------------


def test_openai_gpt_5_pricing_corrected():
    """v0.6.1 fix: gpt-5 was at (5.00, 20.00) speculative; real direct
    OpenAI pricing per Helicone is (1.25, 10.00). Anyone calling gpt-5
    pre-fix had cost_usd over-attributed by ~4x."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-5"] == (1.25, 10.00), (
        "gpt-5 PRICING must reflect direct OpenAI pricing, NOT v0.5.1 "
        "speculative (5.00, 20.00)"
    )


def test_openai_gpt_5_mini_nano_pricing():
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-5-mini"] == (0.25, 2.00)
    assert PRICING["gpt-5-nano"] == (0.05, 0.40)


def test_openai_gpt_4_1_family_pricing():
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-4.1"] == (2.00, 8.00)
    assert PRICING["gpt-4.1-mini"] == (0.40, 1.60)
    assert PRICING["gpt-4.1-nano"] == (0.10, 0.40)


def test_openai_gpt_5_2_pricing():
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["gpt-5.2"] == (1.75, 14.00)
    assert PRICING["gpt-5.2-pro"] == (21.00, 168.00)


def test_openai_default_unchanged_under_bill_on_uncertainty():
    """v0.5.1 _default = gpt-4o-mini for under-bill. v0.6.1 keeps that."""
    from swarph_mesh.adapters.openai import PRICING

    assert PRICING["_default"] == (0.15, 0.60)


# ---------------------------------------------------------------------------
# Anthropic: dated-build aliases + missing tier entries
# ---------------------------------------------------------------------------


def test_anthropic_opus_4_1_added():
    """v0.6.1 NEW: opus-4-1 was missing from adapter PRICING despite
    being live in AIMLAPI catalog. Premium tier ($15/$75)."""
    from swarph_mesh.adapters.claude import PRICING

    assert PRICING["claude-opus-4-1"] == (15.00, 75.00)
    # Dated alias resolves to same price
    assert PRICING["claude-opus-4-1-20250805"] == (15.00, 75.00)


def test_anthropic_sonnet_4_5_added():
    """v0.6.1 NEW: sonnet-4-5 was missing despite being on AIMLAPI."""
    from swarph_mesh.adapters.claude import PRICING

    assert PRICING["claude-sonnet-4-5"] == (3.00, 15.00)
    assert PRICING["claude-sonnet-4-5-20250929"] == (3.00, 15.00)


def test_anthropic_haiku_3_5_added():
    """v0.6.1 NEW: haiku-3-5 had been missing — older cheap tier."""
    from swarph_mesh.adapters.claude import PRICING

    assert PRICING["claude-haiku-3-5"] == (0.80, 4.00)


def test_anthropic_dated_build_aliases_resolve():
    """AIMLAPI catalog uses dated builds (claude-opus-4-5-20251101);
    PRICING now includes them so the cost path doesn't fall through
    to _default."""
    from swarph_mesh.adapters.claude import PRICING

    assert PRICING["claude-opus-4-5-20251101"] == (5.00, 25.00)
    assert PRICING["claude-haiku-4-5-20251001"] == (1.00, 5.00)


# ---------------------------------------------------------------------------
# xAI: grok-4 retirement + grok-4-fast family + alias normalization
# ---------------------------------------------------------------------------


def test_grok_4_fast_pricing():
    from swarph_mesh.adapters.grok import PRICING

    assert PRICING["grok-4-fast-reasoning"] == (0.20, 0.50)
    assert PRICING["grok-4-fast-non-reasoning"] == (0.20, 0.50)


def test_grok_4_3_and_4_20_pricing():
    """v0.6.1: current generation per xAI docs."""
    from swarph_mesh.adapters.grok import PRICING

    assert PRICING["grok-4-3"] == (1.25, 2.50)
    assert PRICING["grok-4-20-0309-reasoning"] == (1.25, 2.50)


def test_grok_4_retirement_notice_present():
    from swarph_mesh.adapters.grok import _GROK_RETIREMENT_NOTICE

    assert "grok-4" in _GROK_RETIREMENT_NOTICE
    assert _GROK_RETIREMENT_NOTICE["grok-4"] == "2026-05-15"


def test_normalize_xai_id_strips_prefix():
    from swarph_mesh.adapters.grok import _normalize_xai_id

    assert _normalize_xai_id("x-ai/grok-4") == "grok-4"
    assert _normalize_xai_id("x-ai/grok-3-beta") == "grok-3"
    assert _normalize_xai_id("x-ai/grok-4-07-09") == "grok-4"
    assert _normalize_xai_id("grok-4-fast-reasoning") == "grok-4-fast-reasoning"


def test_normalize_xai_id_stripping_does_not_break_canonical():
    """Bare IDs pass through unchanged."""
    from swarph_mesh.adapters.grok import _normalize_xai_id

    assert _normalize_xai_id("grok-4") == "grok-4"
    assert _normalize_xai_id("grok-4-3") == "grok-4-3"


def test_grok_compute_cost_resolves_via_normalizer():
    """v0.6.1: cost computation uses normalizer so prefixed IDs
    resolve to PRICING entries."""
    from swarph_mesh.adapters.grok import _compute_cost

    # x-ai/grok-4 should resolve to grok-4 = (5.00, 15.00)
    cost_prefixed = _compute_cost("x-ai/grok-4", 1_000_000, 1_000_000)
    cost_bare = _compute_cost("grok-4", 1_000_000, 1_000_000)
    assert cost_prefixed == cost_bare == 20.00  # 5+15 per 1M tokens


def test_grok_cost_per_token_resolves_via_normalizer():
    from swarph_mesh.adapters.grok import GrokAdapter

    a = GrokAdapter(api_key="fake")
    # Prefixed form should NOT fall through to _default
    assert a.cost_per_token("x-ai/grok-3-beta") == (3.00, 15.00)


# ---------------------------------------------------------------------------
# DeepSeek: slash-prefix normalization
# ---------------------------------------------------------------------------


def test_normalize_deepseek_id_strips_prefix_and_version():
    from swarph_mesh.adapters.deepseek import _normalize_deepseek_id

    assert _normalize_deepseek_id("deepseek/deepseek-chat-v3.1") == "deepseek-chat"
    assert (
        _normalize_deepseek_id("deepseek/deepseek-reasoner-v3.1-terminus")
        == "deepseek-reasoner"
    )
    assert _normalize_deepseek_id("deepseek-v4-flash") == "deepseek-v4-flash"


def test_deepseek_compute_cost_resolves_prefixed():
    from swarph_mesh.adapters.deepseek import _compute_cost

    # deepseek/deepseek-chat-v3.1 should resolve to deepseek-chat = (0.14, 0.28)
    cost_prefixed = _compute_cost(
        "deepseek/deepseek-chat-v3.1", 1_000_000, 1_000_000
    )
    cost_bare = _compute_cost("deepseek-chat", 1_000_000, 1_000_000)
    assert cost_prefixed == cost_bare == pytest.approx(0.14 + 0.28)


def test_deepseek_cost_per_token_resolves_prefixed():
    from swarph_mesh.adapters.deepseek import DeepSeekAdapter

    a = DeepSeekAdapter(api_key="fake")
    # Prefixed AIMLAPI shape resolves to PRICING (not _default)
    assert a.cost_per_token("deepseek/deepseek-chat-v3.1") == (0.14, 0.28)


# ---------------------------------------------------------------------------
# Gemini: 2.0 family additions
# ---------------------------------------------------------------------------


def test_gemini_2_0_family_added():
    from swarph_mesh.adapters.gemini import PRICING

    assert PRICING["gemini-2.0-flash"] == (0.10, 0.40)
    assert PRICING["gemini-2.0-flash-001"] == (0.10, 0.40)


def test_gemini_2_5_family_unchanged():
    """v0.6.1 only adds 2.0; 2.5 family from v0.5.x preserved."""
    from swarph_mesh.adapters.gemini import PRICING

    assert PRICING["gemini-2.5-flash"] == (0.075, 0.30)
    assert PRICING["gemini-2.5-pro"] == (1.25, 5.00)


# ---------------------------------------------------------------------------
# Verified-at metadata present
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# xAI cost reconciliation (v0.6.1 — Management API)
# ---------------------------------------------------------------------------


def test_xai_resolve_management_key_arg_wins(monkeypatch):
    from swarph_mesh.discovery import _resolve_xai_management_key

    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "from-env")
    assert _resolve_xai_management_key("from-arg") == "from-arg"


def test_xai_resolve_management_key_env_fallback(monkeypatch):
    from swarph_mesh.discovery import _resolve_xai_management_key

    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "from-env")
    assert _resolve_xai_management_key(None) == "from-env"


def test_xai_resolve_team_id_env(monkeypatch):
    from swarph_mesh.discovery import _resolve_xai_team_id

    monkeypatch.setenv("XAI_TEAM_ID", "team-abc")
    assert _resolve_xai_team_id(None) == "team-abc"


def test_xai_fetch_returns_empty_when_team_id_missing(monkeypatch):
    """Both XAI_MANAGEMENT_KEY AND XAI_TEAM_ID must be set; missing
    team_id returns [] gracefully (no crash)."""
    from swarph_mesh.discovery import fetch_xai_cost_buckets

    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "fake-mgmt-key")
    monkeypatch.delenv("XAI_TEAM_ID", raising=False)
    out = fetch_xai_cost_buckets(
        start_time="2026-05-01T00:00:00Z", end_time="2026-05-09T00:00:00Z"
    )
    assert out == []


def test_xai_fetch_returns_empty_when_management_key_missing(monkeypatch):
    from swarph_mesh.discovery import fetch_xai_cost_buckets

    monkeypatch.delenv("XAI_MANAGEMENT_KEY", raising=False)
    monkeypatch.setenv("XAI_TEAM_ID", "team-abc")
    out = fetch_xai_cost_buckets(
        start_time="2026-05-01T00:00:00Z", end_time="2026-05-09T00:00:00Z"
    )
    assert out == []


def test_xai_fetch_parses_mocked_response(monkeypatch):
    """xAI's actual response shape (verified against live smoke
    2026-05-09): timeSeries[] with dataPoints[]. Test mirrors the
    real payload so regressions in the parser are caught."""
    import json as _json
    from swarph_mesh.discovery import fetch_xai_cost_buckets

    fake_response = {
        "timeSeries": [
            {
                "group": ["Chat grok-3-mini"],
                "groupLabels": ["Chat grok-3-mini"],
                "dataPoints": [
                    {"timestamp": "2026-05-01T00:00:00Z", "values": [0]},
                    {"timestamp": "2026-05-02T00:00:00Z", "values": [0.45]},
                ],
            },
            {
                "group": ["Chat grok-4-fast-reasoning"],
                "groupLabels": ["Chat grok-4-fast-reasoning"],
                "dataPoints": [
                    {"timestamp": "2026-05-01T00:00:00Z", "values": [0.10]},
                    {"timestamp": "2026-05-02T00:00:00Z", "values": [0.20]},
                ],
            },
        ],
        "limitReached": False,
    }

    captured_body = {}

    def _mock_open(req, timeout=None):
        assert req.get_method() == "POST"
        assert req.headers.get("Authorization") == "Bearer fake-mgmt-key"
        assert "/v1/billing/teams/team-abc/usage" in req.full_url
        body = _json.loads(req.data.decode("utf-8"))
        captured_body.update(body)
        # Verify the analyticsRequest wrapper + camelCase fields +
        # xAI's date format ("YYYY-MM-DD HH:MM:SS" not ISO 8601)
        ar = body["analyticsRequest"]
        assert ar["timeRange"]["startTime"] == "2026-05-01 00:00:00"
        assert ar["timeRange"]["endTime"] == "2026-05-09 00:00:00"
        assert ar["timeRange"]["timezone"] == "Etc/GMT"
        assert ar["timeUnit"] == "TIME_UNIT_DAY"
        assert ar["values"] == [{"name": "usd", "aggregation": "AGGREGATION_SUM"}]
        assert ar["groupBy"] == ["description"]

        class _R:
            def read(self):
                return _json.dumps(fake_response).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "fake-mgmt-key")
    monkeypatch.setenv("XAI_TEAM_ID", "team-abc")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    buckets = fetch_xai_cost_buckets(
        start_time="2026-05-01T00:00:00Z", end_time="2026-05-09T00:00:00Z"
    )
    # Two timestamps in the response (May 1 + May 2); each pivots
    # across both timeSeries groups
    assert len(buckets) == 2
    # May 1: 0 + 0.10 = 0.10 (only grok-4-fast-reasoning had spend)
    assert buckets[0].start_time == "2026-05-01T00:00:00Z"
    assert buckets[0].total_usd == pytest.approx(0.10)
    assert len(buckets[0].line_items) == 1  # 0-value entries skipped
    # May 2: 0.45 + 0.20 = 0.65 across both groups
    assert buckets[1].total_usd == pytest.approx(0.65)
    assert len(buckets[1].line_items) == 2
    # Per-model breakdown preserved in line_items
    descriptions = {li["description"] for li in buckets[1].line_items}
    assert descriptions == {"Chat grok-3-mini", "Chat grok-4-fast-reasoning"}


def test_xai_reconcile_computes_drift(monkeypatch):
    import json as _json
    from swarph_mesh.discovery import reconcile_xai_cost

    fake_response = {
        "timeSeries": [
            {
                "groupLabels": ["Chat grok-4-fast-reasoning"],
                "dataPoints": [
                    {"timestamp": "2026-05-01T00:00:00Z", "values": [0.50]},
                ],
            }
        ],
        "limitReached": False,
    }

    def _mock_open(req, timeout=None):
        class _R:
            def read(self):
                return _json.dumps(fake_response).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "fake")
    monkeypatch.setenv("XAI_TEAM_ID", "team-abc")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    # xAI says we spent 0.50; we claim 0.45 → drift +0.05 = +11.11%
    result = reconcile_xai_cost(
        start_time="2026-05-01T00:00:00Z",
        end_time="2026-05-09T00:00:00Z",
        swarph_attributed_usd=0.45,
    )
    assert result["xai_actual_usd"] == pytest.approx(0.50)
    assert result["swarph_attributed_usd"] == 0.45
    assert result["drift_usd"] == pytest.approx(0.05)
    assert result["drift_pct"] == pytest.approx(11.111, rel=0.001)


def test_xai_fetch_handles_4xx_gracefully(monkeypatch):
    import urllib.error as _ue
    from swarph_mesh.discovery import fetch_xai_cost_buckets

    def _mock_open(req, timeout=None):
        raise _ue.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "bad-key")
    monkeypatch.setenv("XAI_TEAM_ID", "team-abc")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    out = fetch_xai_cost_buckets(
        start_time="2026-05-01T00:00:00Z", end_time="2026-05-09T00:00:00Z"
    )
    assert out == []


# ---------------------------------------------------------------------------
# DeepSeek balance (v0.6.1 — different shape from cost-bucket APIs)
# ---------------------------------------------------------------------------


def test_deepseek_balance_returns_none_no_key(monkeypatch):
    """No API key → graceful None (not crash)."""
    from swarph_mesh.discovery import fetch_deepseek_balance
    import swarph_mesh.adapters.deepseek as _ds

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(_ds, "_resolve_api_key", lambda: None)
    result = fetch_deepseek_balance()
    assert result is None


def test_deepseek_balance_parses_live_shape(monkeypatch):
    """Mocks the actual response shape we observed live (verified
    2026-05-09)."""
    import json as _json
    from swarph_mesh.discovery import DeepSeekBalance, fetch_deepseek_balance

    fake_response = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "19.98",
                "granted_balance": "0.00",
                "topped_up_balance": "19.98",
            }
        ],
    }

    def _mock_open(req, timeout=None):
        # Verify auth + URL shape
        assert req.headers.get("Authorization") == "Bearer fake-deepseek-key"
        assert "/user/balance" in req.full_url

        class _R:
            def read(self):
                return _json.dumps(fake_response).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek-key")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    result = fetch_deepseek_balance()
    assert isinstance(result, DeepSeekBalance)
    assert result.total_balance == 19.98
    assert result.topped_up_balance == 19.98
    assert result.granted_balance == 0.00
    assert result.currency == "USD"
    assert result.is_available is True
    assert result.fetched_at  # ISO timestamp


def test_deepseek_balance_picks_usd_when_multiple_currencies(monkeypatch):
    """If DeepSeek ever returns multiple balance_infos entries (per-
    currency), prefer the USD one."""
    import json as _json
    from swarph_mesh.discovery import fetch_deepseek_balance

    fake_response = {
        "is_available": True,
        "balance_infos": [
            {"currency": "EUR", "total_balance": "5.00",
             "granted_balance": "0", "topped_up_balance": "5.00"},
            {"currency": "USD", "total_balance": "10.00",
             "granted_balance": "0", "topped_up_balance": "10.00"},
        ],
    }

    def _mock_open(req, timeout=None):
        class _R:
            def read(self):
                return _json.dumps(fake_response).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _R()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    result = fetch_deepseek_balance()
    assert result is not None
    assert result.total_balance == 10.00
    assert result.currency == "USD"


def test_deepseek_balance_handles_404_gracefully(monkeypatch):
    """Network/auth failure → None (not crash)."""
    import urllib.error as _ue
    from swarph_mesh.discovery import fetch_deepseek_balance

    def _mock_open(req, timeout=None):
        raise _ue.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "bad-key")
    monkeypatch.setattr("urllib.request.urlopen", _mock_open)

    assert fetch_deepseek_balance() is None


def test_pricing_verified_at_metadata_present():
    """v0.6.1 commits to verified_at metadata so future drift detection
    can flag stale entries."""
    from swarph_mesh.adapters.openai import _OPENAI_PRICING_VERIFIED_AT
    from swarph_mesh.adapters.grok import _GROK_PRICING_VERIFIED_AT
    from swarph_mesh.adapters.gemini import _GEMINI_PRICING_VERIFIED_AT

    assert _OPENAI_PRICING_VERIFIED_AT == "2026-05-09"
    assert _GROK_PRICING_VERIFIED_AT == "2026-05-09"
    assert _GEMINI_PRICING_VERIFIED_AT == "2026-05-09"
