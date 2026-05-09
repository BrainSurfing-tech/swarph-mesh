"""Grok adapter — xAI's OpenAI-compatible API at ``https://api.x.ai/v1``.

Per PLAN.md §3 ship-order #5 ("OpenAI-compatible API, almost-free
additional wrapper if (4) lands cleanly"). xAI ships an
OpenAI-protocol-compatible endpoint, so we use the same
``openai.AsyncOpenAI`` SDK as the OpenAI + (future migrated)
DeepSeek adapters, with ``base_url="https://api.x.ai/v1"``.

API key resolution: ``XAI_API_KEY`` first (canonical), then
``GROK_API_KEY`` (alias), then construction-time kwarg. The dual env
var support is because xAI's docs use both names interchangeably and
tools in the wild ship with either. Resolving both lets existing
setups work without env-var rename.

Models as of 2026-05-08:
- ``grok-4`` (current top tier — multimodal, long context)
- ``grok-3`` (previous top, still in fleet)
- ``grok-3-mini`` (cheap tier — fast classification work)
- ``grok-2`` (legacy; resolves via xAI alias-routing to grok-3)
"""

from __future__ import annotations

import os
import time
from typing import AsyncIterator, Optional

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


XAI_BASE_URL = "https://api.x.ai/v1"


# Per-Mtok pricing (USD), 2026-05-08 baseline.
# Source: https://docs.x.ai/docs/models#pricing
# Per-Mtok pricing (USD).
# v0.6.1 verified against docs.x.ai/docs/models on 2026-05-09.
# IMPORTANT: grok-4 + grok-code-fast-1 are scheduled for retirement
# 2026-05-15 12:00 PT per xAI docs. Old IDs kept in PRICING for back-
# compat with calls in flight pre-retirement; consumers should
# migrate to grok-4-3 / grok-4-1-fast-* / grok-4-20-* by retirement.
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    # — current generation (v0.6.1 catalog) —
    "grok-4-3": (1.25, 2.50),
    "grok-4-20-0309-reasoning": (1.25, 2.50),
    "grok-4-20-0309-non-reasoning": (1.25, 2.50),
    "grok-4-1-fast-reasoning": (0.20, 0.50),
    "grok-4-1-fast-non-reasoning": (0.20, 0.50),
    # AIMLAPI catalog has these without prefix; xAI docs show same
    "grok-4-fast-reasoning": (0.20, 0.50),
    "grok-4-fast-non-reasoning": (0.20, 0.50),
    # — retiring 2026-05-15 (kept for back-compat with in-flight calls) —
    "grok-4": (5.00, 15.00),
    "grok-code-fast-1": (0.20, 1.50),  # estimate; verify before retirement
    # — older generations (still routable today, pricing per v0.5.1) —
    "grok-3": (3.00, 15.00),
    "grok-3-mini": (0.30, 0.50),
    # NOTE (drop DM #716 obs #2): grok-2 was previously listed at
    # (3.00, 15.00) "alias to grok-3 per xAI routing". Removed in
    # v0.5.1 — alias-routed models can silently re-route at the
    # provider's discretion. _default catches grok-2 calls.
    "_default": (3.00, 15.00),
}

_GROK_PRICING_VERIFIED_AT = "2026-05-09"
_GROK_RETIREMENT_NOTICE = {
    # model_id: retirement_date — UserWarning fires when these are called
    # post-retirement-date so operators see the deprecation loud.
    "grok-4": "2026-05-15",
    "grok-code-fast-1": "2026-05-15",
}


def _normalize_xai_id(model: str) -> str:
    """xAI uses two ID formats: bare (``grok-4``) and prefixed
    (``x-ai/grok-4-07-09``). v0.6.1 normalizes by stripping the
    ``x-ai/`` prefix and the dated suffix so PRICING lookups resolve
    against canonical bare IDs.

    Examples:
        x-ai/grok-4 → grok-4
        x-ai/grok-4-07-09 → grok-4 (date suffix stripped)
        x-ai/grok-3-beta → grok-3
        grok-4-fast-reasoning → grok-4-fast-reasoning (no change)
    """
    # Strip x-ai/ prefix
    if model.startswith("x-ai/"):
        model = model[len("x-ai/"):]
    # Strip -beta suffix
    if model.endswith("-beta"):
        model = model[: -len("-beta")]
    # Strip dated suffix like -07-09 (two pairs of digits at end)
    import re as _re

    model = _re.sub(r"-\d{2}-\d{2}$", "", model)
    return model


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict]:
    """Convert ChatMessage list to OpenAI-shape dicts. Trivial — same
    schema. Unknown roles preserved (xAI surfaces validation error
    if it doesn't recognize them)."""
    return [{"role": m.role, "content": m.content} for m in messages]


def _compute_cost(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    # v0.6.1: normalize xAI prefix shapes (x-ai/grok-4 → grok-4) so
    # PRICING lookups resolve regardless of whether caller used the
    # bare or prefixed form. AIMLAPI returns prefixed IDs, xAI's
    # native API returns bare; normalizing here means consumers can
    # pass through either shape without surprise undercounting.
    canonical = _normalize_xai_id(model)
    in_per_mtok, out_per_mtok = PRICING.get(canonical, PRICING["_default"])
    return (
        (input_tokens / 1_000_000.0) * in_per_mtok
        + (output_tokens / 1_000_000.0) * out_per_mtok
    )


def _resolve_api_key() -> Optional[str]:
    """Resolve XAI_API_KEY (canonical) → GROK_API_KEY (alias) → None."""
    return os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")


class GrokAdapter:
    """``LLMAdapter`` for xAI's Grok chat completions API. AsyncOpenAI
    backend (xAI is OpenAI-protocol-compatible); lazy client
    construction; singleton-per-(api_key, base_url).
    """

    name = "grok"
    default_model = "grok-4"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = XAI_BASE_URL,
    ):
        """``api_key`` falls back to ``XAI_API_KEY`` env, then
        ``GROK_API_KEY`` (alias). ``base_url`` defaults to
        https://api.x.ai/v1; override for self-hosted OpenAI-compat
        proxies if needed.
        """
        self._api_key = api_key or _resolve_api_key()
        self._base_url = base_url
        self._client = None  # lazy

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise AdapterError(
                "GrokAdapter requires XAI_API_KEY (or GROK_API_KEY) env, "
                "or api_key kwarg"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise AdapterError(
                "GrokAdapter requires the `openai` package. "
                "Install with: pip install openai>=1.0"
            ) from exc

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
        )
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
        """Native async chat completion via ``AsyncOpenAI`` against
        ``api.x.ai``."""
        del json_schema  # harness trigger only; not enforced natively here

        oai_messages = _to_openai_messages(messages)
        if system_prompt:
            oai_messages = [
                {"role": "system", "content": system_prompt},
                *oai_messages,
            ]

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
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise AdapterError(
                f"GrokAdapter.chat failed for model {model!r}: {exc}"
            ) from exc
        duration_s = time.monotonic() - start

        msg = response.choices[0].message
        text = msg.content or ""

        # Grok returns reasoning_content separately on grok-4 + reasoner
        # variants. Preserve as preamble per PLAN.md §17.3 — same shape
        # as DeepSeek-Reasoner + Claude thinking + OpenAI o-series.
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            text = f"[reasoning]\n{reasoning}\n[/reasoning]\n{text}"

        usage = getattr(response, "usage", None)
        if usage is None:
            input_tokens = output_tokens = 0
            cached_tokens = 0
        else:
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            # xAI exposes cached_tokens on prompt_tokens_details (same
            # shape as OpenAI — they cloned the schema).
            details = getattr(usage, "prompt_tokens_details", None)
            cached_tokens = int(getattr(details, "cached_tokens", 0) or 0) if details else 0

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
                "finish_reason": response.choices[0].finish_reason,
            },
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Streaming v0.5+ stretch alongside the cross-adapter
        ``stream()`` work."""
        raise NotImplementedError(
            "GrokAdapter.stream is v0.5+ stretch; use chat() for now."
        )
        yield ""  # pragma: no cover

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Return (input_per_mtok, output_per_mtok) USD for ``model``.
        v0.6.1: normalizes ``x-ai/`` prefix + dated/beta suffixes via
        ``_normalize_xai_id`` before PRICING lookup."""
        return PRICING.get(_normalize_xai_id(model), PRICING["_default"])

    def list_models(self, *, ttl_seconds: int = 86400):
        """v0.6.0 catalog query — AIMLAPI primary + per-provider fallback."""
        from swarph_mesh.discovery import list_models as _list

        return _list(provider=self.name, ttl_seconds=ttl_seconds)
