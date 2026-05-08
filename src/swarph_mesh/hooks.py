"""Lifecycle hooks per PLAN.md §9.

Five hook points:

* ``pre_call``  — before adapter dispatch
* ``post_call`` — after adapter returns successfully
* ``on_error``  — adapter raised
* ``pre_parse`` — before JSON-mode parse
* ``post_parse`` — after JSON parse (success or failure)

v0.1.0 ships the Hook protocol + a default ``post_call`` hook that
writes an :class:`AttributionEvent` via the configured
:class:`AttributionWriter`. Other hook points are scaffolded as
no-ops so callers can register additional hooks against a stable
shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from swarph_mesh.attribution import (
    AttributionWriter,
    get_default_writer,
    make_event,
)
from swarph_mesh.types import ChatMessage, LLMResponse


@dataclass
class CallContext:
    """Per-call envelope passed through the hook pipeline. Mutable —
    pre_call hooks can rewrite ``messages`` (e.g., redaction layer).
    Adapters MUST treat ``messages`` and ``model`` as inputs only,
    not outputs."""

    provider: str
    model: str
    caller: str
    role: str
    mesh_peer: Optional[str]
    messages: list[ChatMessage]
    system_prompt: Optional[str] = None
    json_schema: Optional[dict] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)


PreCallHook = Callable[[CallContext], Awaitable[None]]
PostCallHook = Callable[[CallContext, LLMResponse], Awaitable[None]]
OnErrorHook = Callable[[CallContext, BaseException], Awaitable[None]]


@dataclass
class HookSet:
    """Collection of hooks for one ``SwarphCall``. Hooks fire in
    registration order. Empty by default — callers register what
    they need."""

    pre_call: list[PreCallHook] = field(default_factory=list)
    post_call: list[PostCallHook] = field(default_factory=list)
    on_error: list[OnErrorHook] = field(default_factory=list)


def attribution_post_call(
    writer: Optional[AttributionWriter] = None,
) -> PostCallHook:
    """Default post-call hook factory: writes one
    :class:`AttributionEvent` per successful call.

    Use the module-level default writer (``FileAttributionWriter``
    by default) if none is provided. Production consumers swap the
    default writer at startup; per-call override is also supported.
    """

    async def _hook(ctx: CallContext, resp: LLMResponse) -> None:
        w = writer or get_default_writer()
        cached_tokens = (resp.raw_response or {}).get("cached_tokens", 0) if resp.raw_response else 0
        event = make_event(
            provider=ctx.provider,
            model=ctx.model,
            role=ctx.role,
            caller=ctx.caller,
            mesh_peer=ctx.mesh_peer,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cached_tokens=int(cached_tokens),
            cost_usd=resp.cost_usd,
            duration_s=resp.duration_s,
            cached=resp.cached,
            error_class=resp.error_class,
        )
        await w.write(event)

    return _hook


def default_hooks(writer: Optional[AttributionWriter] = None) -> HookSet:
    """Return a HookSet with the default attribution post-call hook
    pre-installed. Other slots empty."""
    return HookSet(post_call=[attribution_post_call(writer=writer)])
