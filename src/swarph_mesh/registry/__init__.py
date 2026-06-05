"""swarph-mesh adapter registry — the open adapter contract (P2).

Generalizes the hardcoded ``adapters/_REGISTRY`` so any popular agentic
CLI (node-wrap) or router (lane-register) can be declared + probed via a
uniform :class:`AdapterSpec` record.
"""

from __future__ import annotations

from swarph_mesh.registry.access import get_registry, reset_builtin_registry
from swarph_mesh.registry.indexer import (
    SKUNKWORKS_CAPABILITY,
    capability_payload,
    make_indexer_hook,
)
from swarph_mesh.registry.instances import (
    OPENCLAW_SPEC,
    OPENROUTER_SPEC,
    register_builtin_instances,
)
from swarph_mesh.registry.probes import GateResult, GateStatus, probe_adapter
from swarph_mesh.registry.publish import (
    SKUNKWORKS_FEATURE,
    gateway_publisher,
    spec_to_feature,
)
from swarph_mesh.registry.registry import AdapterRegistry, NoLaneError
from swarph_mesh.registry.spec import AdapterSpec

__all__ = [
    "OPENCLAW_SPEC",
    "OPENROUTER_SPEC",
    "SKUNKWORKS_CAPABILITY",
    "SKUNKWORKS_FEATURE",
    "AdapterRegistry",
    "AdapterSpec",
    "GateResult",
    "GateStatus",
    "NoLaneError",
    "capability_payload",
    "gateway_publisher",
    "get_registry",
    "make_indexer_hook",
    "probe_adapter",
    "register_builtin_instances",
    "reset_builtin_registry",
    "spec_to_feature",
]
