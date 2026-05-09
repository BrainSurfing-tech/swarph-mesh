"""SwarphCall — the public surface per PLAN.md §4.

The single entry point most callers use. Wraps caller validation
(``swarph_shared.validate_caller``), adapter resolution (registry lookup),
lifecycle hook dispatch (pre_call / post_call / on_error), the JSON-mode
harness when ``json_schema`` is provided, and the structured
``LLMResponse`` return.

Example::

    from swarph_mesh import SwarphCall, ChatMessage

    result = await SwarphCall(
        provider="gemini",
        model="gemini-2.5-flash",
        caller="orchestrator.boss",
    ).chat(
        messages=[ChatMessage(role="user", content="hi")],
    )
    print(result.text, result.cost_usd, result.input_tokens)

Caller convention is **enforced** at SwarphCall construction —
invalid caller strings raise ValueError before any adapter dispatch.
That's the defense-in-depth pattern from
``workers/caller_convention.py`` / swarph-shared.
"""

from __future__ import annotations

from typing import Any, Optional

from swarph_shared import validate_caller

from swarph_mesh.adapters import get_adapter
from swarph_mesh.exceptions import SwarphMeshError
from swarph_mesh.hooks import CallContext, HookSet, default_hooks
from swarph_mesh.json_harness import make_retry_callback, parse_with_retry
from swarph_mesh.types import ChatMessage, LLMResponse


class SwarphCall:
    """One configured invocation context. Construct once per call site
    (or once per caller-role and reuse) — adapter + hooks are lazily
    resolved so construction is cheap."""

    def __init__(
        self,
        *,
        provider: str,
        caller: str,
        model: Optional[str] = None,
        role: str = "agents",
        mesh_peer: Optional[str] = None,
        hooks: Optional[HookSet] = None,
        api_key: Optional[str] = None,
    ):
        # Caller-convention enforcement at the public surface — fail
        # loud on invalid callers BEFORE anything else happens. Same
        # discipline as workers/caller_convention.py validates writes.
        validate_caller(caller)

        self.provider = provider
        self.caller = caller
        self.role = role
        self.mesh_peer = mesh_peer
        self._api_key = api_key

        # Adapter resolution is lazy so construction doesn't require
        # API keys for callers that only build but never invoke.
        self._adapter = None

        self.model = model
        # default model deferred to adapter property at first call

        # Default hook set installs the attribution post-call hook.
        # Pass hooks=HookSet() to opt out of attribution.
        self.hooks = hooks if hooks is not None else default_hooks()

    # ------------------------------------------------------------------
    # Lazy adapter access
    # ------------------------------------------------------------------

    @property
    def adapter(self):
        if self._adapter is None:
            self._adapter = get_adapter(self.provider, api_key=self._api_key)
            if self.model is None:
                self.model = self._adapter.default_model
        return self._adapter

    # ------------------------------------------------------------------
    # Public chat surface
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system_prompt: Optional[str] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ) -> LLMResponse:
        """Invoke the adapter with the configured caller / role /
        attribution wiring.

        ``json_schema`` triggers the JSON-mode harness: parse the
        response, retry-once with feedback as a new ``[USER]`` turn
        on parse failure, populate ``LLMResponse.parsed``.

        ``timeout_seconds`` is honored at the asyncio level (wraps
        the adapter call in ``asyncio.wait_for``). v0.1.0 doesn't
        plumb timeouts through the bridge's underlying SDK call —
        an in-flight provider request that exceeds the timeout will
        complete on the SDK side but its result is discarded.

        TODO(v0.2.0, drop PR #1 review carry-forward #1): asyncio
        cancellation is Python-side only — the provider still bills
        for the in-flight call even if we discard the result. Under
        timeout-heavy workloads this creates an unbounded billing
        tail. Thread the timeout through ``bridge.invoke(timeout=...)``
        when the bridge surface supports it; otherwise document the
        cost trade-off explicitly at every call site that uses
        ``timeout_seconds``.
        """
        adapter = self.adapter

        ctx = CallContext(
            provider=self.provider,
            model=self.model or adapter.default_model,
            caller=self.caller,
            role=self.role,
            mesh_peer=self.mesh_peer,
            messages=list(messages),
            system_prompt=system_prompt,
            json_schema=json_schema,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # Pre-call hooks
        for hook in self.hooks.pre_call:
            await hook(ctx)

        # Adapter dispatch
        import asyncio

        async def _do_chat() -> LLMResponse:
            return await adapter.chat(
                messages=ctx.messages,
                model=ctx.model,
                system_prompt=ctx.system_prompt,
                # Don't pass json_schema down — adapter doesn't enforce it.
                temperature=ctx.temperature,
                max_tokens=ctx.max_tokens,
            )

        try:
            if timeout_seconds is not None:
                resp = await asyncio.wait_for(_do_chat(), timeout=timeout_seconds)
            else:
                resp = await _do_chat()
        except BaseException as exc:
            # on_error hooks fire and re-raise. Keep chain intact.
            for hook in self.hooks.on_error:
                try:
                    await hook(ctx, exc)
                except Exception:
                    # Hook errors don't suppress the original.
                    pass
            raise

        # JSON-mode harness if a schema was requested
        if json_schema is not None:
            on_retry = make_retry_callback(
                adapter=adapter,
                base_messages=ctx.messages,
                model=ctx.model,
                system_prompt=ctx.system_prompt,
                temperature=ctx.temperature,
                max_tokens=ctx.max_tokens,
            )
            parsed, error_class = await parse_with_retry(resp.text, on_retry)
            resp.parsed = parsed
            if error_class:
                resp.error_class = error_class

        # Post-call hooks (attribution writer included by default)
        for hook in self.hooks.post_call:
            try:
                await hook(ctx, resp)
            except Exception:
                # Hook errors don't suppress the response — log + continue.
                # Production attribution-writer failures shouldn't lose
                # the user's actual completion.
                import logging

                logging.getLogger(__name__).warning(
                    "post_call hook failed; response returned regardless",
                    exc_info=True,
                )

        return resp


__all__ = ["SwarphCall"]
