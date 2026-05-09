"""Gemini adapter — wraps ``langgraph-genai-bridge`` per PLAN.md §3.

Why the bridge and not raw ``google.genai``: the bridge ships
production-tested Flex tier handling, context caching (Pro tier),
and usage-metadata extraction in a clean abstraction. Re-implementing
those features in this adapter would duplicate ~200 LOC for no
substrate benefit. The bridge is a stable v0.1.5 PyPI package that
both lab and droplet have run in production for weeks.

Cost calculation uses Google's published per-Mtok pricing as of
2026-04-29; update the ``PRICING`` table when Google revises rates.
Flex tier (``flex=True``) gets a 50% rebate per Google's announced
pricing — applied after the base cost computation.

The adapter exposes the swarph_mesh :class:`LLMAdapter` Protocol
shape: async ``chat`` + ``stream`` + sync ``cost_per_token``.
``stream`` is a v0.2.0 stretch (PLAN.md doesn't gate Phase 1 on
streaming; the bridge supports it but we keep the surface simple
for now and raise ``NotImplementedError``).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncIterator, Optional

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


# Gemini per-Mtok pricing (USD).
# v0.6.1 catch-up: extended with 2.0 family + dated builds. Flex tier
# applies a 50% rebate after base computation. Authoritative source
# for live pricing is Google's Cloud Billing Catalog API
# (``swarph_mesh.discovery.fetch_gemini_pricing``), which exposes
# tiered rates including the >128K context multiplier; this static
# table is the fast-path fallback for cost computation when the
# Cloud Billing API isn't configured.
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    # — 2.5 family —
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-flash-lite": (0.019, 0.075),
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-2.5-pro-preview": (1.25, 5.00),
    # — 2.0 family (NEW v0.6.1) —
    "gemini-2.0-flash": (0.10, 0.40),  # base rate; verify-after sentinel needed
    "gemini-2.0-flash-001": (0.10, 0.40),
    # Default fallback used when an unknown model id is requested
    "_default": (0.075, 0.30),
}

_GEMINI_PRICING_VERIFIED_AT = "2026-05-09"


def _to_langchain_messages(messages: list[ChatMessage]) -> list:
    """Convert our ChatMessage list to LangChain BaseMessage list.

    The bridge ``invoke()`` method takes LangChain messages; we
    keep our package's user-facing surface as plain dataclasses
    (no LangChain dep at the public API).
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
    )

    out: list = []
    for m in messages:
        if m.role == "user":
            out.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            out.append(AIMessage(content=m.content))
        elif m.role == "system":
            out.append(SystemMessage(content=m.content))
        else:
            # Unknown role — let the bridge surface the error
            out.append(HumanMessage(content=f"[{m.role}] {m.content}"))
    return out


def _compute_cost(
    model: str, input_tokens: int, output_tokens: int, flex: bool
) -> float:
    """Per-Mtok cost using ``PRICING``. Flex applies 50% rebate."""
    in_per_mtok, out_per_mtok = PRICING.get(model, PRICING["_default"])
    cost = (input_tokens / 1_000_000.0) * in_per_mtok + (
        output_tokens / 1_000_000.0
    ) * out_per_mtok
    if flex:
        cost *= 0.5
    return cost


class GeminiAdapter:
    """``LLMAdapter`` implementation backed by ``langgraph-genai-bridge``."""

    name = "gemini"
    default_model = "gemini-2.5-flash"

    def __init__(
        self,
        api_key: Optional[str] = None,
        flex: bool = True,
    ):
        """``api_key`` falls back to ``GEMINI_API_KEY`` env. ``flex``
        defaults to True per OMEGA's standard production config —
        Flex tier costs 50% less for latency-tolerant workloads."""
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self._flex = flex
        # Per-model bridge instances are cached so we don't re-init
        # the SDK client for every call. Keyed by (model, flex).
        self._bridges: dict[tuple[str, bool], object] = {}

    def _get_bridge(self, model: str, flex: bool) -> object:
        from langgraph_genai_bridge import GenAIBridge

        key = (model, flex)
        if key in self._bridges:
            return self._bridges[key]
        if not self._api_key:
            raise AdapterError(
                "GeminiAdapter requires GEMINI_API_KEY env or api_key kwarg"
            )
        b = GenAIBridge(api_key=self._api_key, model=model, flex=flex)
        self._bridges[key] = b
        return b

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Single multi-turn completion. Calls the bridge via
        ``asyncio.to_thread`` so the sync bridge fits in async code."""
        # json_schema enforcement is the JSON harness's job — this
        # method just returns text. The harness handles the parse +
        # retry orchestration.
        del json_schema  # not used directly here

        lc_messages = _to_langchain_messages(messages)
        bridge = self._get_bridge(model, self._flex)

        start = time.monotonic()
        try:
            ai_message = await asyncio.to_thread(
                bridge.invoke,
                lc_messages,
                system_prompt,
            )
        except Exception as exc:
            duration_s = time.monotonic() - start
            raise AdapterError(
                f"GeminiAdapter.chat failed for model {model!r}: {exc}"
            ) from exc
        duration_s = time.monotonic() - start

        # Extract usage from the AIMessage
        usage = getattr(ai_message, "usage_metadata", None) or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cached_tokens_dict = usage.get("input_token_details", {}) or {}
        cached_tokens = int(cached_tokens_dict.get("cache_read", 0))

        cost = _compute_cost(model, input_tokens, output_tokens, self._flex)

        return LLMResponse(
            text=ai_message.content if isinstance(ai_message.content, str) else str(ai_message.content),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            duration_s=duration_s,
            cached=cached_tokens > 0,
            raw_response={
                "cached_tokens": cached_tokens,
                "model": model,
                "flex": self._flex,
            },
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Token-by-token streaming. Phase 1 v0.1.0 raises
        NotImplementedError; bridge supports streaming, this adapter
        will wire it up in v0.2.0."""
        raise NotImplementedError(
            "GeminiAdapter.stream is v0.2.0 stretch; use chat() for now."
        )
        # Unreachable, but keeps the AsyncIterator return type valid for
        # static analysis.
        yield ""  # pragma: no cover

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Return (input_per_mtok, output_per_mtok) USD for ``model``.
        Flex tier rebate is applied at call time, not in this lookup."""
        return PRICING.get(model, PRICING["_default"])

    def list_models(self, *, ttl_seconds: int = 86400):
        """v0.6.0 catalog query — AIMLAPI primary + per-provider fallback."""
        from swarph_mesh.discovery import list_models as _list

        return _list(provider=self.name, ttl_seconds=ttl_seconds)
