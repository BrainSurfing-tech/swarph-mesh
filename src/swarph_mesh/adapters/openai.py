"""OpenAI adapter — native async via ``openai.AsyncOpenAI``.

Per PLAN.md §3 ship-order #4. Built on ``AsyncOpenAI`` from day one
(per drop's swarph-mesh issue #7 forward-direction note) so we don't
inherit the ``asyncio.to_thread`` threadpool baggage that DeepSeek's
adapter currently carries. The DeepSeek adapter will migrate to
``AsyncOpenAI`` in v0.5+ alongside ``stream()`` infrastructure work.

Why native async over sync-wrapped:

- ``openai.AsyncOpenAI`` is httpx-backed, scales at the connection-pool
  level (much higher than asyncio's default ~32-worker threadpool).
- Per-call timeouts thread through cleanly when the LLMAdapter Protocol
  exposes ``timeout_seconds`` (issue #10).
- Streaming surface (``stream()``) is straightforward via
  ``client.chat.completions.create(stream=True)`` returning an
  async iterator.
"""

from __future__ import annotations

import os
import time
from typing import AsyncIterator, Optional

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


# Per-Mtok pricing (USD), 2026-05-08 baseline.
# Source: https://openai.com/api/pricing/
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3": (10.00, 40.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    # gpt-5 entry held until verified against OpenAI's pricing page.
    # Per drop DM #719: gpt-5 + gpt-5.5 are both live as of ~2026-05-01,
    # so the entry is no longer "rumored/extrapolated" — but the (5.00,
    # 20.00) numbers were pre-launch extrapolation, which may be
    # stale-against-real. Verification deferred to v0.5.2 with online
    # pricing-page check (commander-gated since the verify-then-update
    # cycle benefits from human eyes on OpenAI's pricing tier table).
    "gpt-5": (5.00, 20.00),
    # _default tightening (drop DM #716 obs #3): under-bill-on-uncertainty
    # preference per token-spend memory. Lean toward gpt-4o-mini
    # (0.15/0.60) as the conservative undercount default — if a caller
    # invokes a typo or unknown model that OpenAI happens to accept,
    # we under-attribute rather than over-attribute. Auditor signal
    # "this looks suspiciously cheap" beats "we silently overcharged
    # this caller's budget for an unknown model".
    "_default": (0.15, 0.60),
}


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict]:
    """Convert ChatMessage list to OpenAI-shape dicts. Trivial — same
    schema. Unknown roles preserved (OpenAI surfaces validation error
    if it doesn't recognize them)."""
    return [{"role": m.role, "content": m.content} for m in messages]


def _compute_cost(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    """Per-Mtok cost using ``PRICING``."""
    in_per_mtok, out_per_mtok = PRICING.get(model, PRICING["_default"])
    return (
        (input_tokens / 1_000_000.0) * in_per_mtok
        + (output_tokens / 1_000_000.0) * out_per_mtok
    )


class OpenAIAdapter:
    """``LLMAdapter`` for OpenAI's chat completions API. AsyncOpenAI
    backend; lazy client construction; singleton-per-API-key.
    """

    name = "openai"
    default_model = "gpt-4o"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """``api_key`` falls back to ``OPENAI_API_KEY`` env. ``base_url``
        defaults to OpenAI's stock endpoint; subclass-style override
        lets test fixtures or proxy deployments redirect.
        """
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._base_url = base_url
        self._client = None  # lazy

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise AdapterError(
                "OpenAIAdapter requires OPENAI_API_KEY env or api_key kwarg"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise AdapterError(
                "OpenAIAdapter requires the `openai` package. "
                "Install with: pip install openai>=1.0"
            ) from exc

        kwargs = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncOpenAI(**kwargs)
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
        """Native async chat completion via ``AsyncOpenAI``.

        ``json_schema`` is passed through as a harness trigger only
        (parse + retry orchestration lives in ``json_harness``); this
        adapter does NOT enforce OpenAI's native ``response_format``.
        Phase 5+ can thread ``response_format={"type":"json_object"}``
        when callers want native enforcement.
        """
        del json_schema  # not used directly here

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
                f"OpenAIAdapter.chat failed for model {model!r}: {exc}"
            ) from exc
        duration_s = time.monotonic() - start

        msg = response.choices[0].message
        text = msg.content or ""

        # OpenAI o-series (o1, o3, o4-mini) return reasoning_content separately
        # on certain configurations. Preserve as preamble per PLAN.md §17.3
        # honest-framing — same shape as DeepSeek-Reasoner + Claude thinking.
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
            # OpenAI exposes cached_tokens on prompt_tokens_details for
            # automatic prompt caching audit.
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
        """Token-by-token streaming. v0.5.0 raises NotImplementedError;
        AsyncOpenAI supports streaming natively, this adapter will wire
        it up alongside the cross-adapter ``stream()`` work in v0.5+."""
        raise NotImplementedError(
            "OpenAIAdapter.stream is v0.5+ stretch; use chat() for now."
        )
        yield ""  # pragma: no cover — keeps AsyncIterator shape

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Return (input_per_mtok, output_per_mtok) USD for ``model``."""
        return PRICING.get(model, PRICING["_default"])
