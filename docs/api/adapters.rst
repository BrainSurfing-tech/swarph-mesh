Adapters — ``swarph_mesh.adapters``
====================================

Per-provider implementations of ``LLMAdapter`` plus the registry surface (``get_adapter``, ``register_adapter``).

**Canonical adapter names** (Tier 1 stability — reserved across major versions):

- ``gemini``
- ``deepseek``
- ``claude``
- ``openai``
- ``grok``

The registry is intentionally global and singleton. Tests must wrap fixture-driven registrations in ``reset_registry()`` try/finally to avoid singleton pollution across test boundaries.

.. automodule:: swarph_mesh.adapters
   :members:
   :show-inheritance:
