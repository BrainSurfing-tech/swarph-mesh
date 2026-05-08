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
import os
import time
from typing import AsyncIterator, Optional

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.types import ChatMessage, LLMResponse


DEEPSEEK_BASE_URL = "https://api.deepseek.com"


# Per-Mtok pricing (USD), 2026-05-08 baseline.
# Pricing source: https://api-docs.deepseek.com/quick_start/pricing/
PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_per_mtok, output_per_mtok)
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),  # 75%-off promo; revert to (1.74, 3.48) when promo ends
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


def _compute_cost(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    """Per-Mtok cost using ``PRICING``. DeepSeek doesn't have Flex
    tier — pricing is flat per model."""
    in_per_mtok, out_per_mtok = PRICING.get(model, PRICING["_default"])
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
        """Return (input_per_mtok, output_per_mtok) USD for ``model``."""
        return PRICING.get(model, PRICING["_default"])


def _resolve_api_key() -> Optional[str]:
    """Resolve DEEPSEEK_API_KEY from env, then from
    ``/home/ubuntu/deepseek/.env`` (the legacy tool's config file shape).

    Returns ``None`` if no key found; the adapter raises a friendly
    AdapterError on first invoke when ``None``.
    """
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    legacy_env = "/home/ubuntu/deepseek/.env"
    try:
        from pathlib import Path

        p = Path(legacy_env)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except (OSError, UnicodeDecodeError):
        pass
    return None
