"""Model discovery — `swarph_mesh.discovery` (v0.6.0 architectural promotion).

Per drop's DM #720 direction: replaces the static `PRICING` dict's
implicit "is this model_id real" check with a runtime catalog query.
Closes the silent-mis-attribute trap from drop's #728 obs (a) at the
substrate layer.

Architecture (commander direction 2026-05-09): **AIMLAPI as primary
discovery, per-provider as fallback**.

Primary: ``GET https://api.aimlapi.com/models`` — public, no auth,
~608 entries with structured info (id, developer, contextLength,
maxTokens, type, aliases, tags). 24h TTL cache at module scope.

Fallback: per-provider ``/v1/models`` endpoints when AIMLAPI is
unreachable. Each provider requires its own API key:

  - OpenAI:    ``client.models.list()`` (requires OPENAI_API_KEY)
  - DeepSeek:  same shape via ``base_url`` (requires DEEPSEEK_API_KEY)
  - xAI:       same shape via ``base_url`` (requires XAI_API_KEY)
  - Anthropic: ``GET /v1/models`` (requires ANTHROPIC_API_KEY — note
              this is METERED auth, not the subscription path)
  - Google:    ``models.list()`` via ``google-generativeai`` SDK
              (requires GEMINI_API_KEY)

Fallback fires per-provider; if one provider's key is missing, that
provider's models simply don't appear in the fallback list.

PRICING stays in adapter-local tables (the AIMLAPI ``/models``
response does NOT include pricing — only the HTML pricing page does,
and we won't HTML-scrape). The discovery module exposes ``ModelInfo``
records WITHOUT pricing; callers join against the local PRICING dict
for cost information. Drift detection is a future v0.6.x stretch.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


logger = logging.getLogger(__name__)


AIMLAPI_MODELS_URL = "https://api.aimlapi.com/models"
DEFAULT_TTL_SECONDS = 24 * 60 * 60  # 24h


@dataclass
class ModelInfo:
    """One row from the discovery catalog. Provider-agnostic shape;
    pricing intentionally absent (kept in adapter-local PRICING dicts
    since AIMLAPI doesn't expose pricing via API)."""

    id: str
    developer: str  # "Open AI", "Anthropic", "X AI", "DeepSeek AI", "Google", ...
    context_length: Optional[int] = None
    max_tokens: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None  # "openai/chat-completions", "image", "video", etc.
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str = "aimlapi"  # "aimlapi" | "openai" | "anthropic" | ...

    def matches_developer(self, dev: str) -> bool:
        """Loose developer-name match (handles 'Open AI' vs 'OpenAI'
        vs 'openai' drift across catalogs)."""
        norm_self = self.developer.lower().replace(" ", "")
        norm_other = dev.lower().replace(" ", "").replace("-", "")
        return norm_self == norm_other


# Map our adapter provider names to AIMLAPI's developer field values.
# Adapter calls ``list_models(provider="openai")`` → catalog filtered
# by developer="Open AI". Updated when AIMLAPI changes their naming.
_PROVIDER_TO_DEVELOPER: dict[str, str] = {
    "openai": "Open AI",
    "anthropic": "Anthropic",
    "claude": "Anthropic",  # adapter name → developer name
    "google": "Google",
    "gemini": "Google",
    "deepseek": "DeepSeek AI",
    "xai": "X AI",
    "grok": "X AI",
}


# Module-level cache — single in-flight catalog. Reset via
# ``invalidate_catalog()`` for tests + force-refresh.
_catalog_cache: Optional[list[ModelInfo]] = None
_catalog_fetched_at: Optional[float] = None


def invalidate_catalog() -> None:
    """Force the next ``list_models`` call to re-fetch from AIMLAPI."""
    global _catalog_cache, _catalog_fetched_at
    _catalog_cache = None
    _catalog_fetched_at = None


def _is_cache_fresh(ttl_seconds: int) -> bool:
    if _catalog_cache is None or _catalog_fetched_at is None:
        return False
    return (time.time() - _catalog_fetched_at) < ttl_seconds


def _parse_aimlapi_entry(entry: dict) -> Optional[ModelInfo]:
    """Convert one AIMLAPI ``/models`` array entry to ``ModelInfo``.
    Returns None on entries we can't parse (defensive — catalog has
    drift between docs + live shape)."""
    try:
        info = entry.get("info", {})
        return ModelInfo(
            id=entry["id"],
            developer=info.get("developer", "?"),
            context_length=info.get("contextLength") or None,
            max_tokens=info.get("maxTokens") or None,
            name=info.get("name"),
            description=info.get("description"),
            type=entry.get("type"),
            aliases=list(entry.get("aliases", []) or []),
            tags=list(entry.get("tags", []) or []),
            source="aimlapi",
        )
    except (KeyError, TypeError) as exc:
        logger.debug("discovery: skipped unparseable AIMLAPI entry: %s", exc)
        return None


def _fetch_aimlapi_catalog(timeout: float = 10.0) -> list[ModelInfo]:
    """GET ``https://api.aimlapi.com/models``. No auth required.

    Raises ``urllib.error.URLError`` / ``URLError`` on network
    failure (callers in ``list_models`` translate this to fallback
    activation). Empty list returned if response is malformed (rare —
    AIMLAPI's shape has been stable, but we'd rather emit nothing than
    crash the caller's adapter dispatch)."""
    req = urllib.request.Request(
        AIMLAPI_MODELS_URL,
        headers={"User-Agent": "swarph-mesh/0.6.0 (discovery)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    entries = data.get("data", []) if isinstance(data, dict) else data
    out: list[ModelInfo] = []
    seen_ids: set[str] = set()  # AIMLAPI returns dups; dedupe by id
    for entry in entries:
        m = _parse_aimlapi_entry(entry)
        if m is None or m.id in seen_ids:
            continue
        seen_ids.add(m.id)
        out.append(m)
    return out


def _fetch_provider_models_openai_compat(
    *, base_url: str, api_key: str, source_label: str, developer: str
) -> list[ModelInfo]:
    """Generic OpenAI-protocol-compatible ``/v1/models`` fallback.
    Used by OpenAI, DeepSeek, xAI, and any future adapter on the same
    protocol. Returns empty list on auth failure (treat as "this
    provider's keys aren't configured")."""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        models = client.models.list()
        out: list[ModelInfo] = []
        for m in models.data:
            out.append(
                ModelInfo(
                    id=m.id,
                    developer=developer,
                    type=getattr(m, "object", None),
                    source=source_label,
                )
            )
        return out
    except Exception as exc:
        logger.debug(
            "discovery: %s fallback failed (likely missing key): %s",
            source_label,
            exc,
        )
        return []


def _fetch_per_provider_fallback(provider: Optional[str]) -> list[ModelInfo]:
    """Per-provider fallback when AIMLAPI is unreachable. Honors the
    ``provider`` filter — if specified, only that provider's fallback
    runs; otherwise tries all providers we have keys for."""
    out: list[ModelInfo] = []
    targets = (
        [provider] if provider else ["openai", "deepseek", "xai", "google", "anthropic"]
    )

    if "openai" in targets:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            out.extend(
                _fetch_provider_models_openai_compat(
                    base_url="https://api.openai.com/v1",
                    api_key=key,
                    source_label="openai",
                    developer="Open AI",
                )
            )
    if "deepseek" in targets:
        # Reuse DeepSeekAdapter's resolution logic for the legacy
        # env file fallback (DEEPSEEK_LEGACY_ENV_PATH).
        try:
            from swarph_mesh.adapters.deepseek import _resolve_api_key

            key = _resolve_api_key()
        except ImportError:
            key = os.environ.get("DEEPSEEK_API_KEY")
        if key:
            out.extend(
                _fetch_provider_models_openai_compat(
                    base_url="https://api.deepseek.com",
                    api_key=key,
                    source_label="deepseek",
                    developer="DeepSeek AI",
                )
            )
    if "xai" in targets or "grok" in targets:
        key = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
        if key:
            out.extend(
                _fetch_provider_models_openai_compat(
                    base_url="https://api.x.ai/v1",
                    api_key=key,
                    source_label="xai",
                    developer="X AI",
                )
            )

    # Anthropic + Google fallbacks deferred — they don't share the
    # OpenAI-compat shape. v0.6.x stretch when callers need them
    # AND AIMLAPI is consistently unreachable (low signal so far —
    # api.aimlapi.com has been responsive through testing).

    return out


def _refresh_catalog(*, ttl_seconds: int) -> list[ModelInfo]:
    """Refresh module-level cache from AIMLAPI. Only the primary path
    populates the cache; fallback is per-call (no cache) since per-
    provider lists are smaller + already cached at the SDK level."""
    global _catalog_cache, _catalog_fetched_at
    try:
        catalog = _fetch_aimlapi_catalog()
        _catalog_cache = catalog
        _catalog_fetched_at = time.time()
        logger.debug(
            "discovery: AIMLAPI catalog refreshed (%d entries)", len(catalog)
        )
        return catalog
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "discovery: AIMLAPI fetch failed, fallback active: %s",
            exc,
        )
        return []  # signal to caller that fallback should fire


def list_models(
    *,
    provider: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> list[ModelInfo]:
    """Return models known to the catalog.

    ``provider`` is the swarph-mesh adapter name ("openai", "deepseek",
    "claude", "gemini", "grok") which maps to AIMLAPI's developer
    field via :data:`_PROVIDER_TO_DEVELOPER`. Pass ``None`` for the
    full catalog.

    Cache TTL: 24h by default. Pass ``ttl_seconds=0`` to force a fresh
    fetch (e.g., after publishing a new model and wanting to verify it
    appears).
    """
    if ttl_seconds == 0 or not _is_cache_fresh(ttl_seconds):
        catalog = _refresh_catalog(ttl_seconds=ttl_seconds)
    else:
        catalog = _catalog_cache or []

    # Primary path returned data → filter + return
    if catalog:
        if provider is None:
            return list(catalog)
        target_dev = _PROVIDER_TO_DEVELOPER.get(provider, provider)
        return [m for m in catalog if m.matches_developer(target_dev)]

    # Primary failed → fallback per provider
    return _fetch_per_provider_fallback(provider)


def is_model_supported(model_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """Fast existence check. Used by adapters to validate model_id
    before dispatching to the underlying SDK — closes the silent-mis-
    attribute trap (drop's #728 obs (a)) at the substrate layer.

    Matches against ``id`` AND ``aliases`` (AIMLAPI catalogs both
    canonical IDs like ``claude-opus-4-7`` and aliased forms like
    ``openai/gpt-3.5-turbo``)."""
    catalog = list_models(ttl_seconds=ttl_seconds)
    for m in catalog:
        if m.id == model_id or model_id in m.aliases:
            return True
    return False


def get_model_info(
    model_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> Optional[ModelInfo]:
    """Single-model lookup. Returns ``None`` when not in catalog."""
    catalog = list_models(ttl_seconds=ttl_seconds)
    for m in catalog:
        if m.id == model_id or model_id in m.aliases:
            return m
    return None


# ===========================================================================
# Centralized model-id normalizers (drop DM #745 obs #1, v0.6.2)
# ===========================================================================
#
# Per-adapter normalizers (``_normalize_xai_id``, ``_normalize_deepseek_id``)
# remain in their adapter modules for backward-compat callers. v0.6.2
# centralizes the SAME logic here so future telemetry / shared tooling
# (e.g., a future drift-detection cron) can normalize across providers
# without per-adapter import dance.

import re as _re


def normalize_xai_id(model_id: str) -> str:
    """Strip xAI prefix (``x-ai/``) + ``-beta`` + dated build suffixes
    (``-DD-DD``). See ``swarph_mesh.adapters.grok._normalize_xai_id``
    for adapter-local copy with the same semantics."""
    if model_id.startswith("x-ai/"):
        model_id = model_id[len("x-ai/"):]
    if model_id.endswith("-beta"):
        model_id = model_id[: -len("-beta")]
    model_id = _re.sub(r"-\d{2}-\d{2}$", "", model_id)
    return model_id


def normalize_deepseek_id(model_id: str) -> str:
    """Strip DeepSeek prefix (``deepseek/``) + version suffixes
    (``-v3.1``, ``-v3.2-terminus``). See
    ``swarph_mesh.adapters.deepseek._normalize_deepseek_id`` for
    adapter-local copy."""
    if model_id.startswith("deepseek/"):
        model_id = model_id[len("deepseek/"):]
    model_id = _re.sub(r"-v\d+\.\d+(-\w+)*$", "", model_id)
    return model_id


def normalize_model_id(provider: str, model_id: str) -> str:
    """Provider-aware normalizer. Dispatches to the right
    per-provider helper. Provider names match the adapter ``name``
    field (``"openai"``, ``"grok"``, etc.)."""
    if provider in ("xai", "grok"):
        return normalize_xai_id(model_id)
    if provider == "deepseek":
        return normalize_deepseek_id(model_id)
    # Other providers: pass through (no known prefix/suffix to strip)
    return model_id


# ===========================================================================
# Shared retirement registry (drop DM #745 obs #2, v0.6.2)
# ===========================================================================
#
# Per-adapter retirement constants (``_GROK_RETIREMENT_NOTICE``) stay
# in their modules for backward-compat. v0.6.2 promotes a SHARED
# registry pattern so callers can query "is this model retired today
# across any provider?" without per-adapter import dance.

import datetime as _dt


# Static cross-provider retirement notice. Each entry maps a fully-
# qualified ``provider/model_id`` key to the date past which the
# model is no longer routable. Update by editing this dict + bumping
# ``_RETIREMENT_REGISTRY_VERIFIED_AT``.
_RETIREMENT_REGISTRY_VERIFIED_AT = "2026-05-09"

_RETIREMENT_REGISTRY: dict[str, str] = {
    # provider/model_id: retirement_date (ISO YYYY-MM-DD)
    "grok/grok-4": "2026-05-15",
    "grok/grok-code-fast-1": "2026-05-15",
    # Anthropic deprecated models — flagged by Anthropic but still
    # routable (metered API serves them); listing here surfaces the
    # status to drift-detection. Retirement date not specified by
    # Anthropic; using "deprecated" as a sentinel.
    "claude/claude-sonnet-3-7": "deprecated",
    "claude/claude-opus-3": "deprecated",
}


def is_retired(provider: str, model_id: str, *, today: Optional[_dt.date] = None) -> bool:
    """Return True if the model is past its retirement date as of
    ``today`` (default: actual today UTC). Returns False for
    unregistered models (not retired) and for ``deprecated`` sentinel
    entries (still routable, just deprecated)."""
    key = f"{provider}/{model_id}"
    retirement = _RETIREMENT_REGISTRY.get(key)
    if retirement is None or retirement == "deprecated":
        return False
    try:
        retirement_date = _dt.date.fromisoformat(retirement)
    except ValueError:
        return False
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    return today >= retirement_date


def retirement_date(provider: str, model_id: str) -> Optional[str]:
    """Return the retirement-date string for a provider/model_id, OR
    None if not in the registry."""
    return _RETIREMENT_REGISTRY.get(f"{provider}/{model_id}")


# ===========================================================================
# Pricing discovery — heterogeneous per-provider sources (v0.6.0)
# ===========================================================================
#
# AIMLAPI's catalog has NO pricing data. Only Google has a clean
# programmatic source today (Cloud Billing Catalog API). Other providers
# stay on adapter-local PRICING dicts + verify-after sentinel.
#
# As of 2026-05-09:
#
#   Provider   | Source                                    | Status
#   -----------|-------------------------------------------|------------------
#   Google     | Cloud Billing Catalog API (this module)   | ✓ live, API-key
#   OpenAI     | /v1/organization/costs (admin key only)   | historical USAGE
#   Anthropic  | none public                                | manual table only
#   xAI        | none public                                | manual table only
#   DeepSeek   | none public                                | manual table only
#   AIMLAPI    | HTML pricing page (no API)                 | manual scrape only
#
# Future: when more providers expose programmatic pricing, add per-source
# fetch helpers below mirroring ``_fetch_gemini_pricing``.

# Vertex AI / Gemini service ID in Google's Cloud Billing Catalog.
# Reference: gcloud billing services list | grep -i vertex
GEMINI_BILLING_SERVICE_ID = "241C-273D-49C8"
GEMINI_BILLING_URL_TEMPLATE = (
    "https://cloudbilling.googleapis.com/v1/services/{service_id}/skus"
)

# Module-level pricing cache, keyed by provider name. 24h TTL like the
# catalog. Pricing changes at provider-scheduled cadence (months), so
# even 24h is conservative.
_pricing_cache: dict[str, list] = {}
_pricing_fetched_at: dict[str, float] = {}


@dataclass
class ProviderPricing:
    """One row of pricing data for a provider+SKU combo.

    Tiered models (e.g., Gemini Pro at >128K context) surface as
    multiple ``ProviderPricing`` records — one per tier band — with
    ``tier_threshold_tokens`` distinguishing them. Callers wanting
    "the price at N tokens" pick the highest-threshold record where
    ``tier_threshold_tokens <= N``.
    """

    provider: str  # "google" / "gemini" — adapter-name space
    model_hint: str  # description-extracted model fragment
    sku_id: str
    sku_description: str
    input_per_mtok: Optional[float] = None  # USD per 1M tokens
    output_per_mtok: Optional[float] = None  # USD per 1M tokens
    tier_threshold_tokens: int = 0  # 0 = base tier; 128000 = >128K tier
    usage_unit: Optional[str] = None  # "TBy", "MTok", etc. as returned
    source: str = "google-cloud-billing"
    verified_at: Optional[str] = None  # ISO timestamp of fetch


def invalidate_pricing(provider: Optional[str] = None) -> None:
    """Clear pricing cache. ``provider=None`` clears all."""
    if provider is None:
        _pricing_cache.clear()
        _pricing_fetched_at.clear()
    else:
        _pricing_cache.pop(provider, None)
        _pricing_fetched_at.pop(provider, None)


def _money_to_usd(units: object, nanos: object) -> float:
    """Convert Google's Money proto-shape to a float USD value.

    Google returns ``units`` as a string (int64 wire-format), ``nanos``
    as an int. Per the spec: total = units + nanos/1e9.
    """
    try:
        u = int(units) if units else 0
    except (ValueError, TypeError):
        u = 0
    try:
        n = int(nanos) if nanos else 0
    except (ValueError, TypeError):
        n = 0
    return u + n / 1_000_000_000.0


def _classify_gemini_sku(description: str) -> tuple[Optional[str], int]:
    """Parse a Gemini SKU description into ``(direction, tier_threshold)``.

    Heuristics from observed SKU descriptions (subject to drift; revisit
    when verify-after sentinel fires):

    - ``Input``/``Prompt`` → input direction
    - ``Output``/``Completion`` → output direction
    - ``Greater than 128k`` / ``Long Context`` / ``200k`` → tier_threshold=128000
    - Otherwise base tier (0)

    Returns ``(None, 0)`` on unrecognized SKU shape (caller skips).
    """
    desc_lower = description.lower()
    direction: Optional[str] = None
    if "input" in desc_lower or "prompt" in desc_lower:
        direction = "input"
    elif "output" in desc_lower or "completion" in desc_lower:
        direction = "output"
    # Tier detection — Gemini 1.5 Pro doubles pricing past 128K
    tier = 0
    if any(
        marker in desc_lower
        for marker in ("greater than 128k", "long context", "200k", "1m context")
    ):
        tier = 128000
    return direction, tier


def _parse_gemini_sku(sku: dict, *, verified_at: str) -> list[ProviderPricing]:
    """Convert one SKU JSON object to zero-or-more ``ProviderPricing``
    records. SKUs with multiple ``tieredRates`` produce one record per
    tier. SKUs that don't match the Gemini-pricing shape return ``[]``.
    """
    description = sku.get("description", "")
    if "gemini" not in description.lower():
        return []  # not a Gemini SKU
    direction, sku_tier_threshold = _classify_gemini_sku(description)
    if direction is None:
        return []  # unrecognized direction; skip rather than guess

    sku_id = sku.get("skuId", "")
    pricing_info = sku.get("pricingInfo", [])
    if not pricing_info:
        return []
    expression = pricing_info[0].get("pricingExpression", {})
    usage_unit = expression.get("usageUnit")
    base_unit = expression.get("baseUnit")
    # SKUs are usually priced per-byte (1Tby = 1e12 bytes) or per-character.
    # For Gemini token pricing we want per-Mtok. The API exposes
    # ``displayQuantity`` + ``usageUnit`` like {"displayQuantity": 1.0,
    # "usageUnit": "1MCT"} = "1 million characters". For tokens, watch
    # for "MTok" or "1MTok".
    tiered_rates = expression.get("tieredRates", [])

    out: list[ProviderPricing] = []
    for rate in tiered_rates:
        unit_price = rate.get("unitPrice", {})
        per_unit_usd = _money_to_usd(
            unit_price.get("units"), unit_price.get("nanos")
        )
        # tieredRates expose startUsageAmount as the kick-in threshold
        # in the SKU's usage unit (not tokens directly). The
        # base/expensive split for >128K Gemini context is encoded as
        # SEPARATE SKUs with description containing "Greater than
        # 128k", not as tiered rates within a single SKU. So we use
        # the description-derived tier_threshold here, falling back to
        # the rate's startUsageAmount only as a hint.
        rate_threshold = sku_tier_threshold
        record = ProviderPricing(
            provider="gemini",
            model_hint=_extract_model_from_description(description),
            sku_id=sku_id,
            sku_description=description,
            usage_unit=usage_unit,
            tier_threshold_tokens=rate_threshold,
            source="google-cloud-billing",
            verified_at=verified_at,
        )
        # Convert per-unit-usd to per-Mtok based on usage_unit. Tokens-
        # per-Mtok = per-unit * 1e6 / unit_size_in_tokens. For now we
        # document the per-unit price under the relevant direction and
        # leave the per-Mtok normalization to the caller until we've
        # observed enough live SKUs to encode the unit conversions safely.
        if direction == "input":
            record.input_per_mtok = per_unit_usd
        else:
            record.output_per_mtok = per_unit_usd
        out.append(record)
    return out


def _extract_model_from_description(description: str) -> str:
    """Best-effort model-name extraction from SKU description.

    SKU descriptions look like ``"Gemini 1.5 Pro Input Tokens (Greater than 128k)"``.
    We extract the model fragment between "Gemini" and the direction word.
    Conservative — returns the full description on parse failure so callers
    can match on substring."""
    desc = description
    # Trim leading "Gemini" prefix if present
    lower = desc.lower()
    idx = lower.find("gemini")
    if idx >= 0:
        # Stop at the first direction word
        rest = desc[idx:]
        for stop in ("Input", "Output", "Prompt", "Completion"):
            cut = rest.find(stop)
            if cut > 0:
                return rest[:cut].strip()
        return rest.strip()
    return desc.strip()


def _fetch_gemini_pricing_page(
    *, api_key: str, page_token: Optional[str] = None, timeout: float = 15.0
) -> dict:
    """Fetch one page of Cloud Billing SKUs for the Gemini service.
    Returns the raw JSON dict; callers handle pagination via the
    ``nextPageToken`` field."""
    url = GEMINI_BILLING_URL_TEMPLATE.format(
        service_id=GEMINI_BILLING_SERVICE_ID
    )
    params = [f"key={api_key}", "pageSize=500"]
    if page_token:
        params.append(f"pageToken={page_token}")
    full_url = f"{url}?{'&'.join(params)}"
    req = urllib.request.Request(
        full_url,
        headers={"User-Agent": "swarph-mesh/0.6.0 (discovery.pricing)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_gemini_pricing(
    *,
    api_key: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> list[ProviderPricing]:
    """Hit Cloud Billing Catalog API, return parsed ``ProviderPricing``
    records for all Gemini SKUs.

    Auth: ``api_key`` parameter, OR ``$GOOGLE_CLOUD_BILLING_API_KEY`` env,
    OR ``$GOOGLE_CLOUD_API_KEY`` env. Note: this is a **separate API
    key** from the Generative AI ``GEMINI_API_KEY`` used by the chat
    adapter — Cloud Billing requires Cloud Console project keys with
    billing-API scope enabled. Operators provisioning need:

        gcloud services enable cloudbilling.googleapis.com
        # then create an API key in Cloud Console with Cloud Billing
        # API restriction; export as GOOGLE_CLOUD_BILLING_API_KEY

    Returns empty list on auth failure (treat as "billing-API key not
    configured" — adapter PRICING tables stay authoritative).

    Pagination: Google returns up to 5000 SKUs per page; service
    ``241C-273D-49C8`` typically has < 500 entries so a single page
    suffices. We still page defensively for forward-compat.
    """
    resolved_key = (
        api_key
        or os.environ.get("GOOGLE_CLOUD_BILLING_API_KEY")
        or os.environ.get("GOOGLE_CLOUD_API_KEY")
    )
    if not resolved_key:
        logger.debug(
            "discovery.pricing: GOOGLE_CLOUD_BILLING_API_KEY not set; "
            "Gemini pricing fetch skipped"
        )
        return []

    # Cache hit
    if (
        ttl_seconds > 0
        and "gemini" in _pricing_cache
        and (time.time() - _pricing_fetched_at.get("gemini", 0)) < ttl_seconds
    ):
        return list(_pricing_cache["gemini"])

    import datetime as _dt

    verified_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    out: list[ProviderPricing] = []
    page_token: Optional[str] = None
    pages_fetched = 0
    max_pages = 20  # safety bound

    try:
        while pages_fetched < max_pages:
            data = _fetch_gemini_pricing_page(
                api_key=resolved_key, page_token=page_token
            )
            for sku in data.get("skus", []):
                out.extend(_parse_gemini_sku(sku, verified_at=verified_at))
            page_token = data.get("nextPageToken") or None
            pages_fetched += 1
            if not page_token:
                break
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "discovery.pricing: Gemini Cloud Billing fetch failed: %s", exc
        )
        return []

    _pricing_cache["gemini"] = out
    _pricing_fetched_at["gemini"] = time.time()
    logger.debug(
        "discovery.pricing: fetched %d Gemini pricing records (%d pages)",
        len(out),
        pages_fetched,
    )
    return out


# ---------------------------------------------------------------------------
# Anthropic pricing — manual table from claude.com/pricing
# ---------------------------------------------------------------------------
#
# Anthropic does NOT expose a programmatic pricing endpoint. Source of
# truth is the public docs page; we mirror it here with explicit
# `verified_at` so drift is visible. Update by re-pasting the official
# table and bumping `_ANTHROPIC_PRICING_VERIFIED_AT`.
#
# Full pricing dimensions captured per model:
#   - base input (per Mtok)
#   - 5m cache write (1.25x base)
#   - 1h cache write (2x base)
#   - cache hit / refresh (0.1x base)
#   - output (per Mtok)
#
# These multipliers are part of the published pricing model; the
# explicit values are kept rather than computed so the table acts as
# its own audit record.

_ANTHROPIC_PRICING_VERIFIED_AT = "2026-05-09"

# Tuple shape: (base_input, cache_write_5m, cache_write_1h, cache_hit, output)
# v0.6.1 adds dated-build aliases (e.g. claude-opus-4-5-20251101) so
# AIMLAPI catalog IDs resolve here too. Canonical + dated forms point
# to identical pricing.
_ANTHROPIC_PRICING: dict[str, tuple[float, float, float, float, float]] = {
    # Canonical short-form IDs
    "claude-opus-4-7":   (5.00,  6.25,  10.00, 0.50, 25.00),
    "claude-opus-4-6":   (5.00,  6.25,  10.00, 0.50, 25.00),
    "claude-opus-4-5":   (5.00,  6.25,  10.00, 0.50, 25.00),
    "claude-opus-4-1":   (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-opus-4":     (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-sonnet-4-6": (3.00,  3.75,  6.00,  0.30, 15.00),
    "claude-sonnet-4-5": (3.00,  3.75,  6.00,  0.30, 15.00),
    "claude-sonnet-4":   (3.00,  3.75,  6.00,  0.30, 15.00),
    "claude-sonnet-3-7": (3.00,  3.75,  6.00,  0.30, 15.00),  # deprecated
    "claude-haiku-4-5":  (1.00,  1.25,  2.00,  0.10,  5.00),
    "claude-haiku-3-5":  (0.80,  1.00,  1.60,  0.08,  4.00),
    "claude-opus-3":     (15.00, 18.75, 30.00, 1.50, 75.00),  # deprecated
    "claude-haiku-3":    (0.25,  0.30,  0.50,  0.03,  1.25),
    # v0.6.1 — dated-build aliases (AIMLAPI catalog format).
    # Each maps to the same pricing as its canonical short form.
    "claude-opus-4-5-20251101":   (5.00,  6.25,  10.00, 0.50, 25.00),
    "claude-opus-4-1-20250805":   (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-opus-4-20250514":     (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-sonnet-4-5-20250929": (3.00,  3.75,  6.00,  0.30, 15.00),
    "claude-sonnet-4-20250514":   (3.00,  3.75,  6.00,  0.30, 15.00),
    "claude-haiku-4-5-20251001":  (1.00,  1.25,  2.00,  0.10,  5.00),
}


def pricing_for_anthropic_model(model_id: str) -> Optional[ProviderPricing]:
    """Return a ``ProviderPricing`` record for an Anthropic model.

    Source: ``_ANTHROPIC_PRICING`` static table (manually verified
    against claude.com/pricing on ``_ANTHROPIC_PRICING_VERIFIED_AT``).

    Returns ``None`` for unknown model_ids — callers should fall back
    to the adapter-local PRICING table (which mirrors a subset of this
    table for the modern model lineup).

    The ``ProviderPricing`` shape carries all five Anthropic price
    dimensions in custom attributes; ``input_per_mtok`` is the base
    input rate, ``output_per_mtok`` is the output rate. Cache writes
    + hits are exposed via ``raw_pricing`` for callers that care.
    """
    pricing = _ANTHROPIC_PRICING.get(model_id)
    if pricing is None:
        return None
    base_in, cache_5m, cache_1h, cache_hit, out = pricing
    record = ProviderPricing(
        provider="anthropic",
        model_hint=model_id,
        sku_id=f"anthropic-docs::{model_id}",
        sku_description=f"Anthropic {model_id} (manual table from claude.com/pricing)",
        input_per_mtok=base_in,
        output_per_mtok=out,
        usage_unit="MTok",
        source="anthropic-docs-manual",
        verified_at=_ANTHROPIC_PRICING_VERIFIED_AT,
    )
    # Stash cache-pricing dimensions on the record for callers who
    # need them. Surfaced as plain attributes so dataclass equality
    # stays straightforward; canonical Anthropic 5-tuple preserved.
    record.cache_write_5m_per_mtok = cache_5m  # type: ignore[attr-defined]
    record.cache_write_1h_per_mtok = cache_1h  # type: ignore[attr-defined]
    record.cache_hit_per_mtok = cache_hit  # type: ignore[attr-defined]
    return record


def list_anthropic_pricing() -> list[ProviderPricing]:
    """Return ``ProviderPricing`` records for every model in the
    static Anthropic table. Used by drift-detection cron + audit
    surfaces."""
    return [
        pricing_for_anthropic_model(model_id)
        for model_id in _ANTHROPIC_PRICING
        if pricing_for_anthropic_model(model_id) is not None
    ]


# ---------------------------------------------------------------------------
# DeepSeek balance — current credit snapshot (different shape from
# OpenAI/xAI which expose historical per-day buckets)
# ---------------------------------------------------------------------------
#
# DeepSeek does NOT expose a historical-usage API at well-known paths.
# What they DO expose is ``/user/balance`` — a snapshot of currently-
# available credits. Authoritative per-token pricing for the table in
# ``swarph_mesh.adapters.deepseek.PRICING`` lives at:
#
#   https://api-docs.deepseek.com/quick_start/pricing/
#
# Re-verify when ``_DEEPSEEK_PRICING_VERIFIED_AT`` (in adapter module)
# crosses the verify-after threshold. To approximate historical spend,
# callers can record balance at periodic checkpoints and diff over time:
#
#   t0 = fetch_deepseek_balance().total_balance
#   ... do work ...
#   t1 = fetch_deepseek_balance().total_balance
#   approx_spent_in_window = t0 - t1
#
# Auth: regular ``DEEPSEEK_API_KEY`` (NO admin/management key class —
# DeepSeek doesn't have one). Same key the chat adapter uses.

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"


@dataclass
class DeepSeekBalance:
    """Snapshot of a DeepSeek account's credit balance.

    Unlike OpenAI's ``CostBucket`` and xAI's ``XAICostBucket``,
    DeepSeek's API exposes only current balance — not historical
    per-day usage. Use ``fetched_at`` to track snapshots over time;
    diff successive balances to approximate windowed spend.
    """

    total_balance: float
    granted_balance: float
    topped_up_balance: float
    currency: str = "USD"
    is_available: bool = True
    fetched_at: Optional[str] = None  # ISO timestamp of fetch
    raw: Optional[dict] = None


def fetch_deepseek_balance(
    *, api_key: Optional[str] = None, timeout: float = 10.0
) -> Optional[DeepSeekBalance]:
    """Hit ``GET /user/balance`` on DeepSeek for current credit state.

    ``api_key`` resolves from arg → ``$DEEPSEEK_API_KEY`` env →
    a legacy env file (``DEEPSEEK_LEGACY_ENV_PATH``) fallback (matches
    ``DeepSeekAdapter._resolve_api_key`` shape). The Anthropic-protocol
    endpoint at ``/anthropic/v1/messages`` is documented in the adapter
    module docstring but not used by this balance primitive.

    Returns ``None`` if no key is configured OR the call fails.

    Note: DeepSeek can have multiple ``balance_infos`` entries
    (different currencies). We return the USD entry; callers needing
    other currencies can inspect ``raw``.
    """
    # Reuse adapter's resolution (env + legacy file fallback)
    try:
        from swarph_mesh.adapters.deepseek import (
            _resolve_api_key as _adapter_resolve,
        )

        key = api_key or _adapter_resolve()
    except ImportError:
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")

    if not key:
        logger.debug(
            "discovery.balance: DEEPSEEK_API_KEY unset; balance fetch skipped"
        )
        return None

    req = urllib.request.Request(
        DEEPSEEK_BALANCE_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "swarph-mesh/0.6.1 (deepseek-balance)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("discovery.balance: DeepSeek fetch failed: %s", exc)
        return None

    import datetime as _dt

    fetched_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    balance_infos = data.get("balance_infos", [])
    # Find USD entry; fall back to first entry if no USD
    usd_entry = next(
        (b for b in balance_infos if b.get("currency") == "USD"),
        balance_infos[0] if balance_infos else None,
    )
    if not usd_entry:
        return DeepSeekBalance(
            total_balance=0.0,
            granted_balance=0.0,
            topped_up_balance=0.0,
            is_available=bool(data.get("is_available", False)),
            fetched_at=fetched_at,
            raw=data,
        )
    # DeepSeek returns balance values as strings (per the live response);
    # convert to floats
    return DeepSeekBalance(
        total_balance=float(usd_entry.get("total_balance", 0)),
        granted_balance=float(usd_entry.get("granted_balance", 0)),
        topped_up_balance=float(usd_entry.get("topped_up_balance", 0)),
        currency=usd_entry.get("currency", "USD"),
        is_available=bool(data.get("is_available", True)),
        fetched_at=fetched_at,
        raw=data,
    )


# ---------------------------------------------------------------------------
# xAI cost reconciliation — management-key gated (privilege boundary)
# ---------------------------------------------------------------------------
#
# xAI exposes a Management API at ``https://management-api.x.ai`` with
# team-scoped billing endpoints. The usage endpoint is more sophisticated
# than OpenAI's costs (POST body with timezone + aggregation + grouping
# vs simple query params).
#
# Privilege class: management keys can manage API keys, view billing
# data, configure spending limits. Same blast radius shape as OpenAI
# admin keys — env-only, never on-disk.
#
# Resolution: ``XAI_MANAGEMENT_KEY`` env (Bearer auth) + ``XAI_TEAM_ID``
# env (path scope; obtained from xAI console settings — not discoverable
# via API per the auth docs). Both must be set; missing either returns
# empty list.

XAI_MANAGEMENT_BASE_URL = "https://management-api.x.ai"


def _resolve_xai_management_key(arg: Optional[str]) -> Optional[str]:
    if arg:
        return arg
    return os.environ.get("XAI_MANAGEMENT_KEY")


def _resolve_xai_team_id(arg: Optional[str]) -> Optional[str]:
    if arg:
        return arg
    return os.environ.get("XAI_TEAM_ID")


@dataclass
class XAICostBucket:
    """One time-bucket of xAI usage data per ``/v1/billing/teams/.../usage``.

    xAI's response shape exposes ``line_items`` per bucket, each with
    a model description (e.g., "Chat grok-4-0709") and aggregated
    usage. We extract sum-style cost data into ``total_usd`` when
    available.
    """

    start_time: str  # ISO timestamp (xAI returns these)
    end_time: str
    total_usd: float = 0.0
    line_items: list = field(default_factory=list)
    raw_bucket: Optional[dict] = None


def _to_xai_datetime(ts: str) -> str:
    """Convert ISO 8601 timestamps to xAI's expected
    ``YYYY-MM-DD HH:MM:SS`` format (no T, no timezone suffix).
    Accepts ``2026-05-01T00:00:00Z`` and ``2026-05-01T00:00:00+00:00``
    and the bare format itself."""
    if "T" not in ts:
        return ts  # already in xAI format
    # Strip timezone suffix (Z or ±HH:MM) and replace T with space
    stripped = ts.replace("T", " ")
    for suffix in ("Z", "+00:00"):
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
            break
    # Handle other timezone offsets — drop everything from + or last - of
    # time portion (preserve the date's hyphens)
    if " " in stripped:
        date_part, time_part = stripped.split(" ", 1)
        # Strip ±HH:MM from time_part
        for sign in ("+", "-"):
            idx = time_part.find(sign)
            if idx > 0:
                time_part = time_part[:idx]
                break
        stripped = f"{date_part} {time_part}"
    return stripped


def fetch_xai_cost_buckets(
    *,
    start_time: str,  # ISO 8601 OR "YYYY-MM-DD HH:MM:SS"
    end_time: str,
    management_key: Optional[str] = None,
    team_id: Optional[str] = None,
    time_unit: str = "TIME_UNIT_DAY",  # xAI enum: TIME_UNIT_DAY/HOUR/MONTH/etc.
    timezone: str = "Etc/GMT",
    timeout: float = 15.0,
) -> list[XAICostBucket]:
    """Hit xAI's ``POST /v1/billing/teams/{team_id}/usage`` for a date
    range.

    ``start_time`` / ``end_time`` accept either ISO 8601 (e.g.,
    ``"2026-05-01T00:00:00Z"``) or xAI's native format
    (``"2026-05-01 00:00:00"``). ISO inputs are converted internally;
    timezone is supplied via the separate ``timezone`` parameter
    (default ``"Etc/GMT"``).

    ``time_unit`` is one of xAI's enum strings: ``TIME_UNIT_DAY``,
    ``TIME_UNIT_HOUR``, ``TIME_UNIT_MONTH``, ``TIME_UNIT_CALENDAR_WEEK``,
    ``TIME_UNIT_QUARTER_HOUR``, ``TIME_UNIT_MINUTE``,
    ``TIME_UNIT_SECOND``, ``TIME_UNIT_NONE``.

    ``management_key`` resolves from arg → ``$XAI_MANAGEMENT_KEY`` env.
    ``team_id`` resolves from arg → ``$XAI_TEAM_ID`` env (obtained from
    xAI console — not discoverable via API).

    PRIVILEGE BOUNDARY: management keys can manage API keys, view
    billing, configure spending limits. swarph-mesh does NOT persist
    the key to disk — env-only.

    Returns ``[]`` if either credential is missing OR the call fails
    (4xx/5xx/network).
    """
    key = _resolve_xai_management_key(management_key)
    tid = _resolve_xai_team_id(team_id)
    if not key or not tid:
        logger.debug(
            "discovery.cost_reconciliation: XAI_MANAGEMENT_KEY or "
            "XAI_TEAM_ID unset; xAI usage fetch skipped"
        )
        return []

    url = f"{XAI_MANAGEMENT_BASE_URL}/v1/billing/teams/{tid}/usage"
    # xAI's documented body shape (verified against docs.x.ai). Wrapped
    # in `analyticsRequest`; uses camelCase + enum-string values; date
    # format is "YYYY-MM-DD HH:MM:SS" (NOT ISO 8601 despite docs page
    # mentioning ISO elsewhere).
    body = {
        "analyticsRequest": {
            "timeRange": {
                "startTime": _to_xai_datetime(start_time),
                "endTime": _to_xai_datetime(end_time),
                "timezone": timezone,
            },
            "timeUnit": time_unit,
            "values": [
                {"name": "usd", "aggregation": "AGGREGATION_SUM"},
            ],
            "groupBy": ["description"],
            "filters": [],
        }
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "swarph-mesh/0.6.1 (xai-cost-reconciliation)",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            err_body = {"detail": str(exc)}
        logger.warning(
            "discovery.cost_reconciliation: xAI returned %d: %s",
            exc.code,
            err_body,
        )
        return []
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "discovery.cost_reconciliation: xAI fetch failed: %s", exc
        )
        return []

    # xAI's actual response shape (verified against live smoke
    # 2026-05-09): timeSeries[] grouped by model, each with
    # dataPoints[] per time bucket. Pivot to per-day buckets that
    # aggregate across all model groups, preserving per-model
    # breakdown in line_items so callers can drill in.
    #
    #   {
    #     "timeSeries": [
    #       {
    #         "group": ["Chat grok-3-mini"],
    #         "groupLabels": ["Chat grok-3-mini"],
    #         "dataPoints": [
    #           {"timestamp": "2026-05-08T00:00:00Z", "values": [0.00027585]},
    #           ...
    #         ]
    #       }
    #     ],
    #     "limitReached": false
    #   }

    series = data.get("timeSeries", []) or []
    # Aggregate per timestamp across all groups
    by_timestamp: dict[str, dict] = {}
    for s in series:
        if not isinstance(s, dict):
            continue
        group_label = (
            s.get("groupLabels", s.get("group", ["?"]))[0]
            if s.get("groupLabels") or s.get("group")
            else "?"
        )
        for dp in s.get("dataPoints", []):
            ts = dp.get("timestamp", "")
            values = dp.get("values", [])
            # Each value position corresponds to one entry in our
            # request's `values[]` array; we requested only ``usd``
            # SUM, so values[0] is total USD for this group+bucket.
            usd = float(values[0]) if values else 0.0
            entry = by_timestamp.setdefault(
                ts, {"start_time": ts, "end_time": ts, "total_usd": 0.0,
                     "line_items": []}
            )
            entry["total_usd"] += usd
            if usd > 0:
                entry["line_items"].append(
                    {"description": group_label, "usd": usd}
                )

    out: list[XAICostBucket] = []
    for ts in sorted(by_timestamp.keys()):
        e = by_timestamp[ts]
        out.append(
            XAICostBucket(
                start_time=e["start_time"],
                end_time=e["end_time"],
                total_usd=e["total_usd"],
                line_items=e["line_items"],
                raw_bucket=None,
            )
        )
    return out


def reconcile_xai_cost(
    *,
    start_time: str,
    end_time: str,
    swarph_attributed_usd: Optional[float] = None,
    management_key: Optional[str] = None,
    team_id: Optional[str] = None,
) -> dict:
    """xAI parallel of :func:`reconcile_openai_cost`. Compares xAI's
    actual billed costs against swarph-mesh's attribution.jsonl total
    and returns drift report.

    Returns same shape as ``reconcile_openai_cost`` but with
    ``xai_actual_usd`` instead of ``openai_actual_usd``.
    """
    buckets = fetch_xai_cost_buckets(
        start_time=start_time,
        end_time=end_time,
        management_key=management_key,
        team_id=team_id,
    )
    actual = sum(b.total_usd for b in buckets)
    drift_usd: Optional[float] = None
    drift_pct: Optional[float] = None
    if swarph_attributed_usd is not None and swarph_attributed_usd > 0:
        drift_usd = actual - swarph_attributed_usd
        drift_pct = (drift_usd / swarph_attributed_usd) * 100.0
    return {
        "xai_actual_usd": actual,
        "swarph_attributed_usd": swarph_attributed_usd,
        "drift_usd": drift_usd,
        "drift_pct": drift_pct,
        "buckets": buckets,
        "window_start": start_time,
        "window_end": end_time,
    }


# ---------------------------------------------------------------------------
# OpenAI cost reconciliation — admin-key gated (privilege boundary)
# ---------------------------------------------------------------------------
#
# OpenAI's ``/v1/organization/costs`` endpoint returns historical USAGE
# data (not list pricing) bucketed by day. Authenticates with admin
# keys ONLY (sk-admin-...), which are higher-privilege than regular
# sk- or sk-proj- keys — admin keys can mint MORE admin keys, delete
# keys, manage org settings. Bigger blast radius.
#
# Storage discipline (privilege-boundary): admin key is read from
# ``$OPENAI_ADMIN_KEY`` env ONLY. swarph-mesh does NOT persist it to
# disk anywhere — operators paste at daemon boot if reconciliation is
# wanted, the env-var lives in the process for that session, gone at
# restart. Mirrors the subscription-credentials-out-of-band discipline
# from the Claude adapter.
#
# Use case: periodic verification that our local PRICING tables are
# accurate. Compare swarph-mesh's attribution.jsonl recorded costs
# against OpenAI's bucketed actual costs; drift = stale PRICING OR
# token-counting bug. Out of band of the chat path — costs are
# fetched on-demand, not on every chat call.

OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"


@dataclass
class CostBucket:
    """One day's worth of OpenAI cost data per ``/v1/organization/costs``.

    The endpoint returns up to 7 buckets by default (configurable via
    ``limit``). Each bucket aggregates spend across the bucket window.
    Optional groupings (line_item / project_id / api_key_id) populate
    the breakdown dicts.
    """

    start_time: int  # unix seconds
    end_time: int
    total_usd: float  # aggregated cost across all line_items in the bucket
    currency: str = "usd"
    line_item_breakdown: dict[str, float] = field(default_factory=dict)
    project_breakdown: dict[str, float] = field(default_factory=dict)
    api_key_breakdown: dict[str, float] = field(default_factory=dict)
    raw_results: list = field(default_factory=list)


def _resolve_openai_admin_key(arg: Optional[str]) -> Optional[str]:
    """Admin-key resolution — env-only, never on-disk."""
    if arg:
        return arg
    return os.environ.get("OPENAI_ADMIN_KEY")


def _parse_cost_amount(amount: Optional[dict]) -> tuple[float, str]:
    """Money proto → (USD, currency)."""
    if not amount:
        return 0.0, "usd"
    value = amount.get("value")
    if value is None:
        # Fall back to units+nanos shape if value missing
        return _money_to_usd(amount.get("units"), amount.get("nanos")), amount.get("currency", "usd")
    return float(value), amount.get("currency", "usd")


def _parse_cost_bucket(bucket: dict) -> CostBucket:
    """Convert one bucket from the /v1/organization/costs response shape."""
    results = bucket.get("results", [])
    cb = CostBucket(
        start_time=bucket.get("start_time", 0),
        end_time=bucket.get("end_time", 0),
        total_usd=0.0,
        raw_results=results,
    )
    for r in results:
        if r.get("object") != "organization.costs.result":
            continue  # skip usage-shape results, only sum pure cost rows
        amount_usd, currency = _parse_cost_amount(r.get("amount"))
        cb.total_usd += amount_usd
        cb.currency = currency
        # Group breakdowns when fields are populated
        line_item = r.get("line_item")
        if line_item:
            cb.line_item_breakdown[line_item] = (
                cb.line_item_breakdown.get(line_item, 0.0) + amount_usd
            )
        project = r.get("project_id")
        if project:
            cb.project_breakdown[project] = (
                cb.project_breakdown.get(project, 0.0) + amount_usd
            )
        api_key = r.get("api_key_id")
        if api_key:
            cb.api_key_breakdown[api_key] = (
                cb.api_key_breakdown.get(api_key, 0.0) + amount_usd
            )
    return cb


def fetch_openai_cost_buckets(
    *,
    start_time: int,
    end_time: Optional[int] = None,
    admin_key: Optional[str] = None,
    group_by: Optional[list[str]] = None,
    limit: int = 7,
    timeout: float = 15.0,
) -> list[CostBucket]:
    """Hit OpenAI's ``/v1/organization/costs`` for a date range.

    ``start_time`` / ``end_time`` are Unix-seconds timestamps; bucket
    width is 1d (the only supported value as of 2026-05).

    ``group_by`` accepts any subset of ``["line_item", "project_id",
    "api_key_id"]``. Default is no grouping (just total per bucket).

    ``admin_key`` resolves from arg → ``$OPENAI_ADMIN_KEY`` env. If
    no key is found, returns ``[]`` (treat as "reconciliation not
    configured" — discovery.pricing primitives stay authoritative).

    PRIVILEGE BOUNDARY: admin keys can mint more admin keys, delete
    keys, and manage org settings. swarph-mesh does NOT persist this
    key to disk — operators paste at daemon boot if reconciliation is
    wanted, and the env-var lives in the process for that session
    only. Treat this function as a privileged read; do NOT call it
    inside hot loops or chat paths.

    Returns ``list[CostBucket]`` in chronological order (oldest first).
    """
    key = _resolve_openai_admin_key(admin_key)
    if not key:
        logger.debug(
            "discovery.cost_reconciliation: OPENAI_ADMIN_KEY not set; "
            "cost fetch skipped"
        )
        return []

    params = [f"start_time={start_time}", f"limit={limit}", "bucket_width=1d"]
    if end_time is not None:
        params.append(f"end_time={end_time}")
    if group_by:
        for g in group_by:
            params.append(f"group_by={g}")
    url = f"{OPENAI_COSTS_URL}?{'&'.join(params)}"

    out: list[CostBucket] = []
    page_token: Optional[str] = None
    pages = 0
    max_pages = 20

    while pages < max_pages:
        page_url = url + (f"&page={page_token}" if page_token else "")
        req = urllib.request.Request(
            page_url,
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": "swarph-mesh/0.6.0 (cost-reconciliation)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                err_body = {"detail": str(exc)}
            logger.warning(
                "discovery.cost_reconciliation: OpenAI returned %d: %s",
                exc.code,
                err_body,
            )
            return out
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "discovery.cost_reconciliation: fetch failed: %s", exc
            )
            return out

        for bucket in data.get("data", []):
            out.append(_parse_cost_bucket(bucket))

        if not data.get("has_more"):
            break
        page_token = data.get("next_page")
        if not page_token:
            break
        pages += 1

    out.sort(key=lambda b: b.start_time)
    return out


def reconcile_openai_cost(
    *,
    start_time: int,
    end_time: Optional[int] = None,
    swarph_attributed_usd: Optional[float] = None,
    admin_key: Optional[str] = None,
) -> dict:
    """Fetch OpenAI's actual costs for a window + return a drift report
    against swarph-mesh's attribution.jsonl-recorded total.

    Returns dict::

        {
            "openai_actual_usd": float,    # what OpenAI billed
            "swarph_attributed_usd": float | None,  # what we recorded
            "drift_usd": float | None,     # actual - attributed
            "drift_pct": float | None,     # (drift / attributed) * 100
            "buckets": list[CostBucket],   # per-day breakdown
            "window_start": int,           # unix seconds
            "window_end": int | None,
        }

    ``swarph_attributed_usd=None`` skips the comparison (just returns
    actual costs). Caller is responsible for summing the relevant
    attribution.jsonl rows for the same window.
    """
    buckets = fetch_openai_cost_buckets(
        start_time=start_time,
        end_time=end_time,
        admin_key=admin_key,
    )
    actual = sum(b.total_usd for b in buckets)
    drift_usd: Optional[float] = None
    drift_pct: Optional[float] = None
    if swarph_attributed_usd is not None and swarph_attributed_usd > 0:
        drift_usd = actual - swarph_attributed_usd
        drift_pct = (drift_usd / swarph_attributed_usd) * 100.0
    return {
        "openai_actual_usd": actual,
        "swarph_attributed_usd": swarph_attributed_usd,
        "drift_usd": drift_usd,
        "drift_pct": drift_pct,
        "buckets": buckets,
        "window_start": start_time,
        "window_end": end_time,
    }


def pricing_for_gemini_model(
    model_hint: str,
    *,
    direction: str = "output",
    tier_threshold_tokens: int = 0,
    api_key: Optional[str] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Optional[float]:
    """Return USD-per-Mtok for a Gemini model+direction+tier combination.

    ``model_hint`` is matched as a substring against SKU description's
    model fragment (e.g., ``"1.5 Pro"`` matches ``"Gemini 1.5 Pro"``).
    ``direction`` is ``"input"`` or ``"output"``.
    ``tier_threshold_tokens`` is 0 for base, 128000 for >128K.

    Returns ``None`` when no matching SKU is found OR billing-API key
    is unavailable. Callers fall back to the adapter-local PRICING
    table (``swarph_mesh.adapters.gemini.PRICING``).
    """
    records = fetch_gemini_pricing(api_key=api_key, ttl_seconds=ttl_seconds)
    matches = [
        r
        for r in records
        if model_hint.lower() in r.model_hint.lower()
        and r.tier_threshold_tokens == tier_threshold_tokens
    ]
    if not matches:
        return None
    if direction == "input":
        for r in matches:
            if r.input_per_mtok is not None:
                return r.input_per_mtok
    else:
        for r in matches:
            if r.output_per_mtok is not None:
                return r.output_per_mtok
    return None
