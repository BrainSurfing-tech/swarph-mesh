"""Provider adapters — registry + dispatch.

Phase 1 ships only the Gemini adapter (PLAN.md §3 ship order).
Subsequent phases add DeepSeek / Claude / OpenAI / Grok by adding
modules here + registering them in :func:`get_adapter`.

Adapters are singletons per provider — instantiated on first
request, reused for the rest of the process. This matches the
"adapter registry" shape from PLAN.md §4.
"""

from __future__ import annotations

from typing import Optional

from swarph_mesh.exceptions import UnknownProvider
from swarph_mesh.types import LLMAdapter

# Registry of instantiated singletons (one per provider name)
_REGISTRY: dict[str, LLMAdapter] = {}


def get_adapter(provider: str, *, api_key: Optional[str] = None) -> LLMAdapter:
    """Return the adapter for ``provider``, instantiating on first request.

    Phase 1: only ``"gemini"`` is registered. Other providers raise
    :class:`UnknownProvider`. Phase 4+ adds DeepSeek, Claude, OpenAI, Grok.
    """
    if provider in _REGISTRY:
        return _REGISTRY[provider]

    if provider == "gemini":
        from swarph_mesh.adapters.gemini import GeminiAdapter

        adapter = GeminiAdapter(api_key=api_key)
        _REGISTRY[provider] = adapter
        return adapter

    raise UnknownProvider(
        f"no adapter registered for provider {provider!r}. "
        "Phase 1 ships gemini only; DeepSeek/Claude/OpenAI/Grok ship in Phase 4+."
    )


def register_adapter(provider: str, adapter: LLMAdapter) -> None:
    """Programmatic adapter registration. Test fixtures use this to
    inject mocks; production consumers normally don't need it."""
    _REGISTRY[provider] = adapter


def reset_registry() -> None:
    """Test-only: clear the registry. Not part of the public API."""
    _REGISTRY.clear()


__all__ = ["get_adapter", "register_adapter", "reset_registry"]
