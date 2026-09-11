"""Typed substrate for swarph-mesh — Protocol + dataclasses.

Per PLAN.md §3, every provider implements :class:`LLMAdapter`. Adapters
are model-agnostic; the package keeps the substrate clean and lets each
adapter wrap its own provider SDK in 50-100 LOC.

The shape here is the contract Phase 1+ adapters target. Stable surface
so downstream callers can write
against the Protocol without coupling to a specific provider.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """A single turn in a conversation.

    Roles follow the OpenAI/Anthropic convention:
    ``"user"`` | ``"assistant"`` | ``"system"``.
    """

    role: str = Field(
        ...,
        description='Conversation role. One of "user" | "assistant" | "system".',
    )
    content: str = Field(..., description="Turn text.")


class LLMResponse(BaseModel):
    """Adapter return shape — provider-agnostic.

    Attribution + cost fields are populated by adapters where the
    provider exposes them. ``parsed`` is set when the caller passed a
    ``json_schema`` and the response was parsed via the JSON-mode harness
    (see PLAN.md §7).
    """

    text: str = Field(..., description="Final response text from the LLM.")
    parsed: Optional[dict[str, Any]] = Field(
        None,
        description="Pydantic-parsed response when json_schema was provided.",
    )
    input_tokens: int = Field(0, description="Prompt tokens billed.")
    output_tokens: int = Field(0, description="Completion tokens billed.")
    # #244 token-capture contract. For every Optional[int] field below:
    # None = "this provider did not report it"; 0 = "reported, and it was zero".
    # Adapters must NOT collapse the two — a durable row's None is the signal
    # that a lane cannot see the quantity, not a zero measurement of it.
    thinking_tokens: Optional[int] = Field(
        None,
        description="Reasoning/thinking tokens (billed as output, absent from "
        "``text``). None = provider did not report it; 0 = reported zero.",
    )
    cache_read_tokens: Optional[int] = Field(
        None,
        description="Tokens read from a provider-side cache. "
        "None = provider did not report it; 0 = reported zero.",
    )
    cache_creation_1h: Optional[int] = Field(
        None,
        description="Cache-creation tokens on the 1h-TTL tier. "
        "None = provider did not report it; 0 = reported zero.",
    )
    cache_creation_5m: Optional[int] = Field(
        None,
        description="Cache-creation tokens on the 5m-TTL tier. "
        "None = provider did not report it; 0 = reported zero.",
    )
    cost_usd: float = Field(0.0, description="Adapter-computed cost in USD.")
    cost_basis: Literal["metered", "list", "calculated", "free", "unknown"] = Field(
        "unknown",
        description="How cost_usd was established (#244). "
        '"metered": real billed USD from a metered API. '
        '"list": provider-reported list-price equivalent of subscription '
        "consumption (e.g. claude -p total_cost_usd). "
        '"calculated": derived from a price table — consumer-side; no '
        "swarph-mesh adapter emits this value. "
        '"free": genuinely zero marginal cost AND zero capacity consumed. '
        '"unknown": no figure available — a 0.0 beside "unknown" must never '
        "be aggregated as measured free usage.",
    )
    duration_s: float = Field(..., description="Wall-clock latency in seconds.")
    cached: bool = Field(
        False,
        description="True iff the response was served from a provider-side cache.",
    )
    error_class: Optional[str] = Field(
        None,
        description="Adapter error category, if the call failed gracefully.",
    )
    raw_response: Optional[dict[str, Any]] = Field(
        None,
        description="Provider-specific debug payload — stripped before TSDB write.",
    )


@runtime_checkable
class LLMAdapter(Protocol):
    """Provider adapter Protocol — one implementation per LLM vendor.

    PLAN.md §3 ship order: Gemini → DeepSeek → Claude → OpenAI → Grok.

    Every adapter is a singleton per provider in :class:`SwarphCall`'s
    registry; instantiated once at first use, reused across calls.
    """

    name: str  # "gemini" | "claude" | "deepseek" | "grok" | "openai"
    default_model: str

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict[str, Any]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Single-turn or multi-turn completion. Returns :class:`LLMResponse`."""
        ...

    async def stream(
        self,
        messages: list[ChatMessage],
        model: str,
        **kwargs: object,
    ) -> AsyncIterator[str]:
        """Token-by-token stream. Yields text chunks as they arrive."""
        ...

    def cost_per_token(self, model: str) -> tuple[float, float]:
        """Return ``(input_per_mtok, output_per_mtok)`` in USD."""
        ...

    def list_models(self, *, ttl_seconds: int = 86400) -> list[Any]:
        """Return :class:`swarph_mesh.discovery.ModelInfo` records for
        this provider's catalog. v0.6.0 architectural promotion per
        drop DM #720 + commander direction 2026-05-09.

        Default implementation delegates to ``discovery.list_models``
        with ``provider=self.name``; AIMLAPI primary, per-provider
        fallback when AIMLAPI is unreachable. Adapters MAY override to
        merge their provider's ``/v1/models`` shape directly when
        AIMLAPI lags behind a fresh release (rare; AIMLAPI's catalog
        update cadence has been tight).

        Returns ``list[ModelInfo]`` (not ``list[str]``) so callers get
        ``context_length``, ``max_tokens``, ``aliases``, ``tags``
        without per-adapter hardcoding. ``ttl_seconds=0`` forces a
        fresh fetch.
        """
        ...
