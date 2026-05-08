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
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "grok-4": (5.00, 15.00),
    "grok-3": (3.00, 15.00),
    "grok-3-mini": (0.30, 0.50),
    # NOTE (drop DM #716 obs #2): grok-2 was previously listed at
    # (3.00, 15.00) "alias to grok-3 per xAI routing". Removed in
    # v0.5.1 — alias-routed models can silently re-route at the
    # provider's discretion (e.g., grok-2 → grok-3-mini for cost
    # optimization). Listing them in PRICING locks an assumption
    # that may go stale silently. _default catches grok-2 calls at
    # the same (3.00, 15.00) until xAI exposes a verified pricing
    # API (issue tracked toward v0.6.0 list_models architectural
    # promotion per #720).
    "_default": (3.00, 15.00),
}


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict]:
    """Convert ChatMessage list to OpenAI-shape dicts. Trivial — same
    schema. Unknown roles preserved (xAI surfaces validation error
    if it doesn't recognize them)."""
    return [{"role": m.role, "content": m.content} for m in messages]


def _compute_cost(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    in_per_mtok, out_per_mtok = PRICING.get(model, PRICING["_default"])
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
        """Return (input_per_mtok, output_per_mtok) USD for ``model``."""
        return PRICING.get(model, PRICING["_default"])
