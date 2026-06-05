"""Process-singleton accessor for the built-in :class:`AdapterRegistry` (P2 T4).

COEXISTENCE NOTE: ``AdapterRegistry`` (capability layer, external cli/router
tools) coexists with ``adapters.get_adapter`` (transport factory, built-in SDK
adapters); the built-in SDK adapters are a distinct shape not modeled by
``AdapterSpec``'s cli/router kinds. ``get_registry`` is purely additive — it does
NOT touch ``get_adapter``, ``adapters._REGISTRY``, or any existing adapter.
"""

from __future__ import annotations

from typing import Optional

from swarph_mesh.registry.instances import register_builtin_instances
from swarph_mesh.registry.registry import AdapterRegistry

_REGISTRY_SINGLETON: Optional[AdapterRegistry] = None


def get_registry() -> AdapterRegistry:
    """Return the lazily-built process-singleton :class:`AdapterRegistry`.

    Pre-loaded via :func:`register_builtin_instances` (OpenClaw + OpenRouter).
    ``openclaw_runner`` is left ``None`` — OpenClaw's G1 gate is BLOCKED (not
    FAIL) without a runner, so registration succeeds. Subsequent calls return
    the same instance until :func:`reset_builtin_registry` clears it.
    """
    global _REGISTRY_SINGLETON
    if _REGISTRY_SINGLETON is None:
        reg = AdapterRegistry()
        register_builtin_instances(reg, openclaw_runner=None)
        _REGISTRY_SINGLETON = reg
    return _REGISTRY_SINGLETON


def reset_builtin_registry() -> None:
    """Clear the cached singleton (test hygiene).

    Named ``reset_builtin_registry`` to avoid collision with
    ``adapters.reset_registry``, which resets the unrelated transport-factory
    ``_REGISTRY``.
    """
    global _REGISTRY_SINGLETON
    _REGISTRY_SINGLETON = None
