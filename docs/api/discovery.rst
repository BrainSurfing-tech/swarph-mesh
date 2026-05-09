Discovery — ``swarph_mesh.discovery``
======================================

Cost discovery substrate: AIMLAPI catalog, Gemini Cloud Billing, Anthropic static pricing, OpenAI / xAI / DeepSeek cost reconciliation, model normalization, retirement registry.

**Tier 1 stability.** ``list_models``, ``is_model_supported``, ``get_model_info``, ``fetch_*_pricing``, ``fetch_*_cost_buckets``, ``reconcile_*_cost`` signatures are part of the contract.

**Tier 2 stability.** ``normalize_*_id`` helpers, ``_RETIREMENT_REGISTRY`` keys (consumers may iterate).

**Tier 3 stability.** ``_ANTHROPIC_PRICING`` static table contents.

.. automodule:: swarph_mesh.discovery
   :members:
   :show-inheritance:
