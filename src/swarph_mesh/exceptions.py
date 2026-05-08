"""Exception hierarchy for swarph-mesh."""

from __future__ import annotations


class SwarphMeshError(Exception):
    """Base for all swarph-mesh errors. Catch this if you want to handle
    every failure class uniformly."""


class AdapterError(SwarphMeshError):
    """An adapter call failed. ``error_class`` on the
    :class:`LLMResponse` carries the categorical failure reason; this
    exception type carries the original exception chain for callers who
    want to react to specific provider failures."""


class UnknownProvider(SwarphMeshError):
    """Raised when :class:`SwarphCall` is invoked with a ``provider``
    string that has no registered adapter. Phase 1+ ships adapters
    incrementally — pre-Phase-N callers can hit this for providers that
    haven't landed yet."""
