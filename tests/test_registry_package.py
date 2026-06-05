"""P2 T4 — package-level AdapterRegistry accessor, coexisting with get_adapter.

The capability-layer ``AdapterRegistry`` (external cli/router tools) is exposed
at the package top level via ``get_registry()`` as a process-singleton, WITHOUT
disturbing the existing ``get_adapter`` transport factory.
"""

from __future__ import annotations

from swarph_mesh import get_adapter, get_registry
from swarph_mesh.registry import AdapterRegistry, reset_builtin_registry


def test_both_import_coexistence():
    # Both surfaces import from the package top level (coexistence).
    assert callable(get_registry)
    assert callable(get_adapter)


def test_get_registry_returns_adapter_registry():
    reset_builtin_registry()
    reg = get_registry()
    assert isinstance(reg, AdapterRegistry)


def test_get_registry_is_singleton():
    reset_builtin_registry()
    first = get_registry()
    second = get_registry()
    assert first is second


def test_builtins_are_loaded():
    reset_builtin_registry()
    spec = get_registry().resolve("DEEP_CODE", prompt_bytes=100)
    assert spec.name == "openclaw"


def test_reset_yields_fresh_instance():
    reset_builtin_registry()
    first = get_registry()
    reset_builtin_registry()
    second = get_registry()
    assert first is not second
