"""Provider adapters — registry + dispatch.

Phase 1 shipped Gemini (PLAN.md §3 ship-order #1). Phase 4 adds
DeepSeek (#2); subsequent Phase 4 PRs add Claude (#3), OpenAI (#4),
Grok (#5).

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

    v0.3.0 ships ``"gemini"`` (Phase 1) and ``"deepseek"`` (Phase 4).
    Other providers raise :class:`UnknownProvider`; subsequent Phase 4
    PRs add Claude / OpenAI / Grok.
    """
    if provider in _REGISTRY:
        return _REGISTRY[provider]

    if provider == "gemini":
        from swarph_mesh.adapters.gemini import GeminiAdapter

        adapter = GeminiAdapter(api_key=api_key)
        _REGISTRY[provider] = adapter
        return adapter

    if provider == "deepseek":
        from swarph_mesh.adapters.deepseek import DeepSeekAdapter

        adapter = DeepSeekAdapter(api_key=api_key)
        _REGISTRY[provider] = adapter
        return adapter

    if provider == "claude":
        from swarph_mesh.adapters.claude import ClaudeAdapter

        adapter = ClaudeAdapter(api_key=api_key)
        _REGISTRY[provider] = adapter
        return adapter

    raise UnknownProvider(
        f"no adapter registered for provider {provider!r}. "
        "v0.4.0 ships gemini + deepseek + claude; OpenAI/Grok ship in subsequent "
        "Phase 4 PRs per PLAN.md §3 ship-order."
    )


def register_adapter(provider: str, adapter: LLMAdapter) -> None:
    """Programmatic adapter registration. Test fixtures use this to
    inject mocks; production consumers normally don't need it."""
    _REGISTRY[provider] = adapter


def reset_registry() -> None:
    """Test-only: clear the registry. Not part of the public API."""
    _REGISTRY.clear()


__all__ = ["get_adapter", "register_adapter", "reset_registry"]
