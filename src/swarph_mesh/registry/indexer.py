"""Capability indexer — publish a registered adapter to the swarph graph (P2 T6).

This is the hook that makes a wrapped tool DISCOVERABLE mesh-wide rather than
siloed: on every successful :meth:`AdapterRegistry.register`, the on-register
hook formats the spec into a capability-graph entry and hands it to a pluggable
``transport``. The actual network transport to the live feature registry
(mesh-gateway ``/features`` POST) is wired in a later integration step — T6
builds the payload formatter + the pluggable hook.

The ``invocation`` block tells a discovering client HOW to fire the capability:

* ``cli`` -> a sandboxed MESH_TOOL subprocess (carries its ``input_channel``).
* ``router`` -> an openai-compatible HTTP endpoint.
"""

from __future__ import annotations

from typing import Callable

from swarph_mesh.registry.spec import AdapterSpec

# The FIRST real external capability the indexer is meant to hold: Dory's
# session-isolated Skunkworks pipeline — a mesh-native MESH_TOOL (NOT an
# AdapterSpec, but a capability the indexer surfaces). Reference capability
# for the indexer.
SKUNKWORKS_CAPABILITY = {
    "name": "adversarial-council-pipeline",
    "kind": "mesh_tool",
    "intents": ["MESH_TOOL"],
    "invocation": {
        "channel": "MESH_TOOL",
        "protocol": "LAUNCH_SKUNKWORKS: <seed>",
        "host": "gemini-researcher",
    },
    "note": (
        "Dory's session-isolated Skunkworks daemon — the indexer's reference "
        "capability."
    ),
}


def capability_payload(spec: AdapterSpec) -> dict:
    """Format an :class:`AdapterSpec` into a capability-graph entry.

    Returns a dict ``{name, kind, intents, models (router only — else []),
    billing_class, invocation}``. The ``invocation`` describes HOW a discovering
    client fires it:

    * ``kind=="cli"``   -> ``{"channel": "MESH_TOOL", "input_channel": spec.input_channel}``
    * ``kind=="router"`` -> ``{"channel": "MESH_TOOL", "endpoint": "openai-compat"}``
    """
    if spec.kind == "cli":
        invocation = {"channel": "MESH_TOOL", "input_channel": spec.input_channel}
    else:  # router
        invocation = {"channel": "MESH_TOOL", "endpoint": "openai-compat"}

    return {
        "name": spec.name,
        "kind": spec.kind,
        "intents": list(spec.intents),
        "models": list(spec.models) if spec.kind == "router" else [],
        "billing_class": spec.billing_class,
        "invocation": invocation,
    }


def make_indexer_hook(
    transport: Callable[[dict], None] | None = None,
) -> Callable[[AdapterSpec], None]:
    """Return an on-register hook (``Callable[[AdapterSpec], None]``).

    For each registered spec the hook builds :func:`capability_payload` and
    hands it to ``transport(payload)``. ``transport`` defaults to a no-op that
    appends to an in-memory list ``hook.published`` (so callers/tests can
    inspect what was published without a live network). The real mesh-gateway
    ``/features`` POST is wired in a later integration step.
    """
    published: list[dict] = []

    if transport is None:

        def _transport(payload: dict) -> None:
            published.append(payload)

        transport = _transport

    def hook(spec: AdapterSpec) -> None:
        transport(capability_payload(spec))

    hook.published = published  # type: ignore[attr-defined]
    return hook
