"""DeepSeek adapter — OpenAI-compatible API at ``https://api.deepseek.com``.

Per PLAN.md §3 ship-order #2: "DeepSeek adapter — already have
``/home/ubuntu/deepseek/deepseek.py`` tool-shape; carve the chat path
into adapter shape. ~50 LOC."

DeepSeek's API is OpenAI-protocol-compatible — we use the official
``openai`` SDK with a custom ``base_url``. The shape mirrors the
GeminiAdapter (lazy client construction, sync→async via
``asyncio.to_thread``, Pricing table + cost computation, raw_response
debug payload).

Model lineup as of 2026-05-08:
- ``deepseek-v4-flash`` (default cheap tier — $0.14/$0.28 per Mtok)
- ``deepseek-v4-pro`` (premium — $1.74/$3.48 normal, 75%-off promo until
  the promo ends; PRICING reflects the promo for now since it's still
  active and we already document the rule)
- ``deepseek-chat`` (V3 alias, OpenAI-compat schema, kept for back-compat)
- ``deepseek-reasoner`` (V3 R1 reasoning model — returns
  ``reasoning_content`` separately; we preserve it as preamble text per
  §17.3 honest-framing, same shape as the Claude parser handles
  ``thinking`` blocks)
"""

from __future__ import annotations

import asyncio
import datetime
import os
import time
import warnings
from typing import AsyncIterator, Optional

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


DEEPSEEK_BASE_URL = "https://api.deepseek.com"


# v0.5.1 fix (issue #6): V4-Pro promo expiry safeguard. The 75%-off
# promo runs until DeepSeek ends it; we don't have an authoritative
# end date but past promos have run ~3 months. Set a sentinel that
# triggers a runtime warning when crossed — keeps attribution honest
# even if the PRICING table goes stale-against-real.
V4_PRO_PROMO_VERIFIED_AT = datetime.date(2026, 5, 8)
V4_PRO_PROMO_VERIFY_AFTER = datetime.date(2026, 8, 8)  # 3 months out
V4_PRO_NORMAL_PRICING: tuple[float, float] = (1.74, 3.48)
V4_PRO_PROMO_PRICING: tuple[float, float] = (0.435, 0.87)


def _v4_pro_pricing() -> tuple[float, float]:
    """Return current V4-Pro pricing, warning if the promo verification
    window has lapsed (issue #6 part 2). Returns the promo price
    regardless — caller decides to override via PRICING dict if they
    have fresher data."""
    if datetime.date.today() > V4_PRO_PROMO_VERIFY_AFTER:
        warnings.warn(
            f"deepseek-v4-pro PRICING was last verified on "
            f"{V4_PRO_PROMO_VERIFIED_AT}; the promo may have ended. "
            f"Verify against https://api-docs.deepseek.com/quick_start/pricing/ "
            f"and update PRICING + V4_PRO_PROMO_VERIFIED_AT in adapters/deepseek.py.",
            UserWarning,
            stacklevel=3,
        )
    return V4_PRO_PROMO_PRICING


# Per-Mtok pricing (USD), 2026-05-08 baseline.
# Pricing source: https://api-docs.deepseek.com/quick_start/pricing/
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": _v4_pro_pricing(),  # 75%-off promo; warning fires past verify-after date
    # V3 aliases — same pricing as v4-flash by DeepSeek's current alias-routing
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.14, 0.28),
    "_default": (0.14, 0.28),
}


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict]:
    """Convert ChatMessage list to OpenAI-shape dicts.

    OpenAI/DeepSeek schema is plain dicts ``{"role": ..., "content": ...}``
    so the conversion is trivial. Unknown roles fall through with the
    role string preserved (DeepSeek will surface its own validation error
    rather than us pre-validating).
    """
    return [{"role": m.role, "content": m.content} for m in messages]


def _normalize_deepseek_id(model: str) -> str:
    """v0.6.1: DeepSeek IDs come in two shapes — bare
    (``deepseek-chat``) and prefixed (``deepseek/deepseek-chat-v3.1``).
    AIMLAPI uses prefix; DeepSeek's own API uses bare. We strip the
    ``deepseek/`` prefix and the version suffix for PRICING lookup so
    both shapes resolve to the same canonical entry.

    Examples:
        deepseek/deepseek-chat-v3.1 → deepseek-chat
        deepseek/deepseek-reasoner-v3.1-terminus → deepseek-reasoner
        deepseek/deepseek-v3.2-speciale → deepseek-v3 → _default fallback
        deepseek-v4-flash → deepseek-v4-flash (no change)
    """
    if model.startswith("deepseek/"):
        model = model[len("deepseek/"):]
    # Strip version suffix patterns: -v3.1, -v3.1-terminus, -v3.2-exp,
    # -v3.2-speciale, etc. Anything after -v<digits>.<digits>.
    import re as _re

    model = _re.sub(r"-v\d+\.\d+(-\w+)*$", "", model)
    # Strip non-thinking / non-reasoner / etc. capability suffixes
    # so deepseek-non-thinking → deepseek
    return model


def _compute_cost(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    """Per-Mtok cost using ``PRICING``. DeepSeek doesn't have Flex
    tier — pricing is flat per model. v0.6.1 normalizes prefix shapes
    via ``_normalize_deepseek_id``."""
    canonical = _normalize_deepseek_id(model)
    in_per_mtok, out_per_mtok = PRICING.get(canonical, PRICING["_default"])
    return (
        (input_tokens / 1_000_000.0) * in_per_mtok
        + (output_tokens / 1_000_000.0) * out_per_mtok
    )


class DeepSeekAdapter:
    """``LLMAdapter`` implementation for DeepSeek's OpenAI-compatible API.

    Lazy client construction — no API key needed at instantiation, only
    at first invoke. Singleton-per-(base_url, key) shape mirrors the
    Gemini bridge cache so multiple SwarphCall instances against the
    same DeepSeek account share one client + one connection pool.
    """

    name = "deepseek"
    default_model = "deepseek-v4-flash"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEEPSEEK_BASE_URL,
    ):
        """``api_key`` falls back to ``DEEPSEEK_API_KEY`` env, then
        ``/home/ubuntu/deepseek/.env`` (matching the legacy tool's
        config-file convention so existing setups keep working).
        ``base_url`` defaults to https://api.deepseek.com but can be
        overridden for self-hosted OpenAI-compat endpoints.
        """
        self._api_key = api_key or _resolve_api_key()
        self._base_url = base_url
        self._client = None  # lazy

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise AdapterError(
                "DeepSeekAdapter requires DEEPSEEK_API_KEY env, "
                "/home/ubuntu/deepseek/.env, or api_key kwarg"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AdapterError(
                "DeepSeekAdapter requires the `openai` package. "
                "Install with: pip install openai>=1.0"
            ) from exc
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Single multi-turn completion. Calls the sync OpenAI SDK via
        ``asyncio.to_thread`` to fit the async LLMAdapter shape.

        ``json_schema`` is passed through as a harness trigger only
        (parse + retry orchestration lives in
        :mod:`swarph_mesh.json_harness`); this adapter does not enforce
        DeepSeek's native structured-output mode in v0.3.0. Phase 5+
        could thread DeepSeek's ``response_format={"type": "json_object"}``
        when callers want native enforcement.
        """
        del json_schema  # not used directly here

        # Prepend system prompt if provided (OpenAI/DeepSeek convention)
        oai_messages = _to_openai_messages(messages)
        if system_prompt:
            oai_messages = [{"role": "system", "content": system_prompt}, *oai_messages]

        client = self._get_client()

        kwargs: dict = {
            "model": model,
            "messages": oai_messages,
            "stream": False,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        start = time.monotonic()
        try:
            response = await asyncio.to_thread(
                client.chat.completions.create, **kwargs
            )
        except Exception as exc:
            raise AdapterError(
                f"DeepSeekAdapter.chat failed for model {model!r}: {exc}"
            ) from exc
        duration_s = time.monotonic() - start

        msg = response.choices[0].message

        # Reasoning models (deepseek-reasoner / R1) return reasoning_content
        # separately. Per PLAN.md §17.3 honest-framing — preserve the
        # reasoning trace as preamble text wrapped in [reasoning] markers
        # so any downstream render (REPL, swarph_native session, log) can
        # toggle visibility. Same shape as the Claude parser uses for
        # thinking blocks (parsers/claude.py wraps them in
        # [thinking]\n...\n[/thinking]).
        reasoning = getattr(msg, "reasoning_content", None)
        text = msg.content or ""
        if reasoning:
            text = f"[reasoning]\n{reasoning}\n[/reasoning]\n{text}"

        usage = getattr(response, "usage", None)
        if usage is None:
            input_tokens = output_tokens = 0
            cached_tokens = 0
        else:
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            # DeepSeek exposes prompt_cache_hit_tokens on the usage object
            # for cache-aware billing audit.
            cached_tokens = int(
                getattr(usage, "prompt_cache_hit_tokens", 0)
                or 0
            )

        cost = _compute_cost(model, input_tokens, output_tokens)

        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            duration_s=duration_s,
            cached=cached_tokens > 0,
            raw_response={
                "cached_tokens": cached_tokens,
                "model": model,
                "has_reasoning": reasoning is not None,
            },
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Token-by-token streaming. v0.3.0 raises NotImplementedError;
        DeepSeek's API supports SSE streaming, this adapter will wire
        it up alongside the GeminiAdapter stream() in v0.4.0."""
        raise NotImplementedError(
            "DeepSeekAdapter.stream is v0.4.0 stretch; use chat() for now."
        )
        yield ""  # pragma: no cover — keeps AsyncIterator shape

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Return (input_per_mtok, output_per_mtok) USD for ``model``.
        v0.6.1: normalizes ``deepseek/`` prefix + version suffixes via
        ``_normalize_deepseek_id`` before PRICING lookup."""
        return PRICING.get(_normalize_deepseek_id(model), PRICING["_default"])

    def list_models(self, *, ttl_seconds: int = 86400):
        """v0.6.0 catalog query — AIMLAPI primary + per-provider fallback."""
        from swarph_mesh.discovery import list_models as _list

        return _list(provider=self.name, ttl_seconds=ttl_seconds)


def _resolve_api_key() -> Optional[str]:
    """Resolve DEEPSEEK_API_KEY from env, then from a legacy ``.env``
    file (defaults to ``/home/ubuntu/deepseek/.env`` for lab-OVH backwards
    compatibility; override via ``DEEPSEEK_LEGACY_ENV_PATH`` env var).

    v0.5.1 fix (issue #6): the hardcoded ``/home/ubuntu/deepseek/.env``
    path bit non-lab-OVH consumers — droplet, GPU box, any other host
    that pip-installs ``swarph-mesh`` and doesn't have that exact path
    sees ``AdapterError`` on first invoke even when DEEPSEEK_API_KEY is
    set elsewhere. Order: ``DEEPSEEK_API_KEY`` env → ``$DEEPSEEK_LEGACY_ENV_PATH``
    file → ``/home/ubuntu/deepseek/.env`` file (legacy default) → ``None``.

    Returns ``None`` if no key found; the adapter raises a friendly
    AdapterError on first invoke when ``None``.
    """
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    # Per-host override; falls back to lab-OVH legacy default.
    legacy_env = os.environ.get(
        "DEEPSEEK_LEGACY_ENV_PATH", "/home/ubuntu/deepseek/.env"
    )
    try:
        from pathlib import Path

        p = Path(legacy_env).expanduser()
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except (OSError, UnicodeDecodeError):
        pass
    return None
